# 只读站点 Agent

这是 E06 实施中的独立 Linux 组件：安全目录读取、严格命令校验、耐久本地结果和固定中心地址的出站 HTTPS 客户端已经实现。**平台中心的配对、任务分派、数据集登记和 UI 尚未接通，当前不能用它把远端目录接入平台。** 本地 TLS 测试只验证传输和恢复，不代表完整 E06 验收通过。

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

配置文件和凭据文件必须由运行账户拥有，权限 `0600`；状态目录必须为该账户拥有的 `0700` 真实目录。状态目录不能位于受控根内，受控根也不能位于状态目录内。配置示例中的地址和路径需要替换，凭据由后续中心配对功能提供，不能用个人 API Key 或训练 token 代替。

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

凭据格式为 `agent_id`、`credential_revision`、`token`、`expires_at`，其中到期时间是整数 Unix 秒。配置检查只展示根 handle 与预算，不输出 token、Agent 身份或绝对根路径：

```sh
/opt/aap-storage-agent/bin/aap-storage-agent --config /etc/aap-storage-agent/agent.json --check
```

## 执行与恢复合同

- 文件读取先在受控根内安全打开，完整复制、计算 SHA 并完成来源退出复核，之后才生成可发送的本地 stage。最多 64 MiB，源复制最多 10 秒；阻塞的物理读取须实际结束，超时不会伪装句柄已关闭。
- 单次命令最长有效 120 秒，命令到期后不再发起原图分块，TLS 握手后及发送前重新检查期限，网络请求受剩余有效期限制；已经在途的字节须由中心再次核对期限后决定是否接收。并发配置是上限；当前客户端串行执行，最多一个活动 I/O。多槽调度和业务优先级随中心接入继续实现。
- 结果先耐久落入本地 SQLite，再发送中心。重启后优先重放同一个结果和 stage；`received` 只代表中心收到，关闭证据完整，且 `applied` 或 `rejected` 的身份、命令 digest 和结果 digest 全部匹配并耐久记录后才删除 stage。关闭未决即使被拒绝也保留为 `unsettled`，不得借撤销或过期清除。
- 临时读文件与耐久 outgoing 文件可以短时共存，读取前预留两份空间；单个 stage 与总状态预算均受限。待确认任务数量有上限，终态只保留最近 256 条紧凑回执。本地日志不是第二套平台任务历史。
- 分块最多 256 KiB，聚合带宽限速；断网重试最多五次并有退避，失败保留待确认结果。重新上传相同块依赖中心按命令和偏移去重，不能把“已经发送”当成中心确认水位。
- 确认丢失时可以重发，不能更换同一命令的内容或延长租约。来源已变化时，旧 stage 仍是原内容；新读取必须取得新命令。关闭未决会上报 `closed=false` 并停止进程继续取任务。
- `--once` 执行一轮；`--drain` 只处理待确认结果，不领取新任务。协议不兼容、撤销、鉴权失败和旧 fence 拒绝直接停止，不无限重试。

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

测试包括真实本机 TLS 对端、证书失败、重定向拒绝、丢 ACK 后重启重放、过期命令、根修订、原图摘要和容量／重试上限。测试中的 HTTPS 对端是受控协议夹具，不是平台中心；完整隔离网络、权限、登记和双站点验收继续按 [E06 计划](../../docs/plans/1792661016_dataset-multi-site-agent.md)实施。
