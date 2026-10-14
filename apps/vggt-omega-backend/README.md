# VGGT-Ω 当前帧重建服务

独立的实验性 GPU 容器，为平台生成深度与置信度中间资产。平台后台任务负责真实 LiDAR 校准、筛选以及最终点云发布；工作台读取已发布结果。

## 固定推理配置

| 项目         | 配置                                                                           |
| ------------ | ------------------------------------------------------------------------------ |
| Profile      | `omega-current-mono-512`                                                       |
| 模型源码     | `facebookresearch/vggt-omega`，提交 `48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6` |
| 权重版本     | `facebook/VGGT-Omega`，revision `1041e80fc0e911235d3426b0a3d9a81075111a53`     |
| 权重文件     | `vggt_omega_1b_512.pt`                                                         |
| 权重 SHA-256 | `c02da418b18bb01d0392598d3f6147366bcde1bb70fd08a5e3bf7925b0667934`             |
| 执行形状     | B6S1：当前帧六路相机分别作为独立单图 batch 元素                                |
| 预处理       | 上游 `balanced`，resolution 480，patch 16                                      |
| 输出网格     | 每路 640×352，仅接受预处理后精确匹配该网格的输入比例                           |
| 推理环境     | PyTorch `2.6.0+cu124`，torchvision `0.21.0+cu124`，BF16 CUDA                   |

Profile 名称保留权重变体标识 `512`，实际预处理分辨率为已验证的 480。六路图片必须来自平台冻结的同一当前帧输入集，角色与顺序由调用方固定。服务不跨帧融合，不输出用于替代平台标定的相机位姿。

源码快照位于 `vendor/`，`SOURCE.json` 记录逐文件哈希，启动时校验。更新快照需显式运行：

```bash
python3 apps/vggt-omega-backend/scripts/sync_vendor.py /path/to/clean/vggt-omega
```

当前上游源码和权重适用的许可证见 [随附 LICENSE](vendor/LICENSE)：FAIR Noncommercial Research License。下载权限与许可证适用范围是独立事项，部署方应按实际用途核对该许可证。

## 启动

权重在部署前下载并校验，容器使用只读挂载。镜像不包含权重或 HF 凭据，运行时不依赖交互式 HF 登录。

```bash
export VGGT_OMEGA_CHECKPOINTS_DIR=/absolute/path/to/verified/weights
export VGGT_OMEGA_GPU_DEVICE_ID=GPU-physical-device-uuid
export VGGT_OMEGA_AUTH_TOKEN=your-backend-token
docker compose -f docker-compose.yml -f docker-compose.ml.yml \
  --profile gpu-vggt-omega build vggt-omega-backend
docker compose -f docker-compose.yml -f docker-compose.ml.yml \
  --profile gpu-vggt-omega up -d vggt-omega-backend
```

`VGGT_OMEGA_CHECKPOINTS_DIR` 中必须存在上述权重文件。GPU 必须明确选择一张物理设备，未配置时采用 `none` 并拒绝启动推理。默认启动不启用此 profile，不影响已有模型服务。容器内该设备映射为 `cuda:0`；健康检查同时报告物理设备身份。

平台注册地址可使用 `http://vggt-omega-backend:8006`，宿主调试地址为 `http://127.0.0.1:8006`。注册时选择 token 认证并填写与 `VGGT_OMEGA_AUTH_TOKEN` 相同的值。所有端点遵循 `Authorization: Bearer ...`。空 token 仅适合隔离的本机开发，Compose 只将端口发布到回环地址。

## 接口

`GET /setup` 返回可选的 `point_cloud_densification` 能力：protocol `1`、endpoint `/reconstruct`、固定 profile、六相机数量和输入/输出限额。它不声明框、Mask 或点云标注预测能力。

`POST /reconstruct` 请求：

```json
{
  "protocol_version": "1",
  "request_id": "attempt-1",
  "idempotency_key": "job-frame-input-key",
  "input_digest": "64-lowercase-hex-digest",
  "profile": "omega-current-mono-512",
  "cameras": [
    {
      "role": "CAM_FRONT",
      "image_url": "https://storage/scoped-source-url",
      "sha256": "64-lowercase-hex-digest",
      "width": 1600,
      "height": 900
    }
  ],
  "upload_targets": [
    {
      "role": "CAM_FRONT",
      "depth": { "url": "https://storage/scoped-depth-put-url" },
      "confidence": { "url": "https://storage/scoped-confidence-put-url" }
    }
  ]
}
```

示例省略其余五路，实际两个列表必须各有六项且角色唯一、顺序一致。每个输出必须有独立的短期 PUT URL。可附带 `headers`，仅允许 Content-Type、S3 服务端加密及校验和头。传输只支持 HTTP(S)，不跟随重定向；源图片上限为每路 16 MiB、宽高各 4096 像素。下载后核对原始文件哈希与实际尺寸。

输出逐相机的两个 NPY 文件均为 `<f4`、`[352,640]`，关闭 pickle。回复只包含这些文件的哈希、字节数、形状、预处理变换以及 producer/timing 信息，不包含大 JSON 数组或存储访问凭据。`source_to_output` 与 `output_to_source` 为展平的行优先 3×3 矩阵，采用零基像素中心坐标：

```text
u_out = (u_source - crop_left + 0.5) * scale_x - 0.5 + pad_left
v_out = (v_source - crop_top  + 0.5) * scale_y - 0.5 + pad_top
```

调用方负责验证回复、重新下载并验证暂存数组，完成 LiDAR 校准与最终发布。`input_digest` 绑定平台冻结的输入快照；服务通过它和有序相机哈希/尺寸绑定幂等键，不接收平台数据库账号。

CPU 结果缓存默认最多两组、32 MiB、300 秒，只保留二进制输出和描述信息。重试必须重新提供有效暂存 URL，即使命中缓存也会再次上传。`cache_hit` 表示此次复用了 CPU 输出；它不表示 CUDA 模型是否已常驻。缓存被驱逐或服务重启后允许重新计算，持久幂等与发布仍由平台任务负责。

## GPU 生命周期

一个 HTTP 进程、容量一的共享 `ManagedLruPool`、每次一个重建请求。另一个重建请求收到可重试的 503，不建立第二个任务队列。借用模型、构建、卸载及真实 executor 结束都保持显式跟踪；HTTP 取消不提前释放正在执行的模型或源图临时目录。

`/health`、`/warmup`、`/unload`、`/drain`、`/drain/cancel`、`/lifecycle/mode`、`/lifecycle/reset` 沿用平台 GPU 生命周期协议。`/reconstruct` 使用 `predict` admission scope，在解析业务请求前校验完整 admission headers。未配置权重时 `/health.ready=false`，`/warmup` 返回 503；不支持 CPU fallback。

`VGGT_OMEGA_MANAGED_LIFECYCLE_VERIFIED` 默认 `0`：不发布 `managed_lifecycle` 能力，拒绝切换到 enforce。只有目标镜像、设备与 profile 完成实卡加载、推理、取消、drain、full unload 及显存回落验收后，部署方才能设为 `1`。CPU 合同测试和离线实验的显存记录不能替代这项验收；注册 VRAM 预算需来自目标部署的实测峰值和运行余量。

| 变量                                    | 默认值                                  | 用途                                      |
| --------------------------------------- | --------------------------------------- | ----------------------------------------- |
| `VGGT_OMEGA_WEIGHTS`                    | `/app/checkpoints/vggt_omega_1b_512.pt` | 容器内权重路径                            |
| `VGGT_OMEGA_AUTH_TOKEN`                 | 空                                      | 平台 registry bearer token                |
| `VGGT_OMEGA_BUILD_TIMEOUT`              | `240`                                   | 模型构建等待秒数，超时后 builder 仍被跟踪 |
| `VGGT_OMEGA_TRANSFER_TIMEOUT`           | `60`                                    | 单次 HTTP 传输超时秒数                    |
| `VGGT_OMEGA_IDLE_UNLOAD_SECONDS`        | `600`                                   | 空闲卸载时间，非正值关闭自动卸载          |
| `VGGT_OMEGA_IDLE_CHECK_INTERVAL`        | `60`                                    | 空闲检查间隔秒数                          |
| `VGGT_OMEGA_CACHE_ENTRIES`              | `2`                                     | CPU 输出缓存最大组数，0 关闭              |
| `VGGT_OMEGA_CACHE_BYTES`                | `33554432`                              | CPU 输出缓存最大字节数                    |
| `VGGT_OMEGA_CACHE_TTL`                  | `300`                                   | CPU 输出缓存秒数                          |
| `VGGT_OMEGA_MANAGED_LIFECYCLE_VERIFIED` | `0`                                     | 当前部署的实卡生命周期验收标记            |
| `GPU_LIFECYCLE_VERIFY_KEYS_JSON`        | 空                                      | 平台签名 admission 公钥环                 |

## 本地测试

无需 GPU 或权重即可测试合同、输入完整性、二进制导出、重试与生命周期拒绝路径：

```bash
cd apps/vggt-omega-backend
uv venv .venv --python 3.10
uv pip install --python .venv/bin/python -r requirements.lock pytest pytest-asyncio ruff
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
.venv/bin/ruff check .
```

Pytest 的共享包路径由 `pyproject.toml` 配置。测试结束清理本次 `.venv`、`__pycache__` 与工具缓存；不要删除部署权重或正式生成资产。
