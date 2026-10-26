# 只读站点 Agent

这是 E06 实施中的独立 Linux 组件：安全目录读取、严格命令校验、耐久本地结果、一次性配对及固定中心地址的出站 HTTPS 客户端已经实现。客户端使用中心的配对、命令轮询、分块和结果 ACK 接口；中心扫描、预检登记与平台原图快照已接通既有 owner；管理 UI、Range 和单站点真实隔离链路已接通，完整双站点验收继续实施。**本地 TLS 配对和独立进程恢复测试是组件证据，不代表完整 E06 已完成。**

Agent 不监听入站端口，不加载 API 配置、数据库／Redis 连接、S3 管理凭据或 Celery。操作只有 `probe`、`browse`、`scan`、`read`；目录绝对路径只写在本地配置，中心只能看到根 handle。

## 本地配置检查

为 Agent 使用独立 Python 虚拟环境，安装本仓库三个本地包；不复用或修改 API 虚拟环境的依赖。需要 Python 3.11+、支持 `openat2` 的 Linux x86_64／aarch64。

```sh
python3 -m venv /opt/aap-storage-agent
/opt/aap-storage-agent/bin/pip install \
  ./apps/_shared/filesystem_read \
  ./apps/_shared/site_agent_contract \
  ./apps/storage-agent
```

配置文件、配对文件和凭据文件必须由运行账户拥有，权限 `0600`；状态目录和凭据的直接父目录必须为该账户拥有的 `0700` 真实目录。新建的凭据父目录逐级使用 `0700`，不修改已有共享目录的权限。状态目录不能位于受控根内，受控根也不能位于状态目录内。配置示例中的地址和路径需要替换，身份由中心的一次性配对提供，不能用个人 API Key 或训练 token 代替。

```json
{
  "center_url": "https://annotation.example.com",
  "state_dir": "/var/lib/aap-storage-agent",
  "credential_file": "/etc/aap-storage-agent/identity.json",
  "roots": {
    "ceph-images": { "path": "/mnt/cephfs", "revision": 1 }
  },
  "max_pending": 16,
  "max_state_bytes": 268435456,
  "max_concurrency": 1,
  "bandwidth_bytes_per_second": 8388608
}
```

中心地址只接受 HTTPS origin，不接受用户名、密码、路径、查询和片段。内部 CA 可通过本地 `ca_file` 的绝对路径指定；不提供跳过 TLS 校验的选项，不跟随重定向，不从环境变量读取代理地址。

凭据格式为 `agent_id`、`credential_revision`、`token`、`expires_at`，其中到期时间是整数 Unix 秒。配置检查在配对前即可运行，不要求凭据文件已存在；只展示根 handle 与预算，不输出 token、Agent 身份或绝对根路径：

```sh
/opt/aap-storage-agent/bin/aap-storage-agent --config /etc/aap-storage-agent/agent.json --check
```

## 一次性配对

管理员在中心签发一次性代码后，将下列两个字段保存到运行账户拥有的 `0600` 文件。`agent_id` 必须是 UUID，`pair_code` 只允许 32–256 个字母、数字、下划线或连字符；文件不接受额外字段。代码及身份 token 不放在命令行或环境变量里。

```json
{
  "agent_id": "00000000-0000-0000-0000-000000000001",
  "pair_code": "REPLACE_WITH_CENTER_ONE_TIME_PAIR_CODE"
}
```

```sh
/opt/aap-storage-agent/bin/aap-storage-agent \
  --config /etc/aap-storage-agent/agent.json \
  --pairing-file /etc/aap-storage-agent/pairing.json
```

客户端只向配置的 HTTPS origin 发送一次 `POST /api/v1/site-agents/pair`，正文严格为 `agent_id`、`pair_code`、`protocol_version: 1`，不附带已有身份凭据，不跟随重定向或代理。响应必须严格包含四个凭据字段，身份匹配、修订为正整数，且到期时间在未来 31 天内。验证成功后以 `0600` 临时文件写入，依次完成文件 fsync、原子重命名和父目录 fsync；发布使用 Linux `renameat2(RENAME_NOREPLACE)`，最后一刻出现的已有路径也不会被覆盖，不支持时停止。成功只输出 `agent_paired`。

凭据路径已有普通文件、目录或符号链接时，在发出请求前停止并返回 `credential_file_exists_use_new_path`。轮换需要管理员主动签发新代码，并在配置中明确指定新的 `credential_file` 路径，不会静默覆盖旧身份。配对操作使用父目录独占锁，避免两个本地配对进程重复消耗代码。

同一状态目录还有待确认旧命令时，配对在发请求前返回 `pending_commands_drain_old_config_before_pairing`；先使用旧配置执行 `--drain`，再轮换凭据。配对不会清空旧日志或删除其 stage；本地日志的独占锁也会阻止运行中实例与配对同时操作。

一次性代码不会自动重试。`pairing_response_uncertain_request_new_code`、响应不匹配或凭据写入失败时，中心可能已消费代码；保留现有文件，向管理员核实并重新签发代码，必要时选择新的凭据路径。写入完成但父目录 fsync 失败时，完整凭据可能已存在，不能据此重复覆盖。配对后按站点运维流程移除一次性代码文件。`--pairing-file`、`--check`、`--once` 和 `--drain` 互斥。

## 执行与恢复合同

- 文件读取先在受控根内安全打开，完整复制、计算 SHA 并完成来源退出复核，之后才生成可发送的本地 stage。最多 64 MiB，源复制最多 10 秒；阻塞的物理读取须实际结束，超时不会伪装句柄已关闭。
- 单次命令最长有效 120 秒，命令到期后不再发起原图分块，TLS 握手后及发送前重新检查期限，网络请求受剩余有效期限制；已经在途的字节须由中心再次核对期限后决定是否接收。并发配置是上限；当前客户端串行执行，最多一个活动 I/O。中心执行已登记的并发上限及读取优先级；独立客户端保持串行。
- 结果先耐久落入本地 SQLite，再向中心提交同一个结果元数据。只有结果接口明确返回 `409 agent_content_incomplete` 才发送同一 stage 的分块并重交结果；正常 ACK 或丢 ACK 后的已接收回执不触发重复上传。`received` 只代表中心收到，关闭证据完整，且 `applied` 或 `rejected` 的身份、命令 digest 和结果 digest 全部匹配并耐久记录后才删除 stage。关闭未决即使被拒绝也保留为 `unsettled`，不得借撤销或过期清除。
- 临时读文件与耐久 outgoing 文件可以短时共存，读取前预留两份空间；单个 stage 与总状态预算均受限。待确认任务数量有上限，终态只保留最近 256 条紧凑回执。本地日志不是第二套平台任务历史。
- 分块最多 256 KiB，聚合带宽限速；分块发送期限取命令和凭据到期时间中较早者。断网重试最多五次并有退避，失败保留待确认结果。重新上传相同块依赖中心按命令和偏移去重，不能把“已经发送”当成中心确认水位。
- 确认丢失时可以重发，不能更换同一命令的内容或延长租约。来源已变化时，旧 stage 仍是原内容；新读取必须取得新命令。关闭未决会上报 `closed=false` 并停止进程继续取任务。
- `--once` 执行一轮；`--drain` 只处理待确认结果，不领取新任务。过期凭据仅可向旧命令的结果接口交关闭回执，不能轮询或上传原图；撤销、过期或旧凭据的结果由中心决定是否保存关闭证据并返回 `rejected`。其他鉴权失败、协议不兼容和旧 fence 拒绝直接停止，不无限重试。
- 同一身份的 session UUID 随独占本地日志耐久保存，进程重启后复用，身份修订变化时更新；认证请求使用 `X-Agent-Session` 回报该值。中心可按原 session 重投丢失响应的命令，保留原 ID 和到期时间；session 不构成独立授权。跨进程恢复仍依赖同一身份、命令 digest、耐久结果和中心 ACK，本地日志独占锁阻止两个实例同时操作。

`Dockerfile` 和 `deploy/storage-agent.service` 提供独立部署骨架，中心接通前不要启用为生产服务。容器以 UID/GID 65532 运行，部署时须给配置／凭据正确属主，把来源以只读方式挂载，并将状态目录单独可写挂载；不挂载 Docker socket 或宿主机根目录。systemd 使用独立静态账户 `aap-storage-agent`，文件属主与权限要求相同。

## 组件验证

从仓库根目录使用已有测试虚拟环境、显式源码路径运行，无需安装依赖或访问数据库：

```sh
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=apps/storage-agent:apps/_shared/filesystem_read/src:apps/_shared/site_agent_contract/src \
apps/api/.venv/bin/python -m pytest \
  apps/_shared/filesystem_read/tests \
  apps/_shared/site_agent_contract/tests \
  apps/storage-agent/tests -p no:cacheprovider
```

测试包括真实本机 TLS 配对、私有文件和严格响应、已有身份拒绝、原子落盘失败、证书及重定向拒绝，以及独立 CLI 进程先收到 `received`、重启后重放同一 stage 并收到 `applied`。同时覆盖丢 ACK、过期命令、根修订、原图摘要和容量／重试上限。测试中的 HTTPS 对端是受控协议夹具，不是平台中心；完整隔离网络、权限、登记和双站点验收继续按 [E06 计划](../../docs/plans/1792661016_dataset-multi-site-agent.md)实施。

中心命令携带可选带宽上限，与本地配置取较低值。图片分块和结果 JSON（包含扫描清单）使用同一正文发送预算；HTTP／TLS 头及配对、轮询控制消息除外。重启允许一个有界首片突发。丢失领取响应的过期命令只用于回报关闭，不再次读取来源或延长原期限。
