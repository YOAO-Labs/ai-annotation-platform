---
audience: [ops, developer, project_admin]
type: how-to
status: experimental
last_reviewed: 2026-10-14
---

# VGGT-Ω 当前帧点云补强

该实验为标注提供可切换的 RGB 辅助点云。真实 LiDAR 始终保留，模型结果不参与
点拾取、测量、自动贴合、标注或导出。默认关闭，用户使用见
[点云视图](../user-guide/workbench/pointcloud-view)。

## 部署

1. 在目标环境应用数据库迁移；为 API 和普通 Celery worker 配置
   `POINT_CLOUD_AI_ENHANCEMENT_ENABLED=true`，刷新使用旧源码的 worker 和 beat。
2. 指定 `VGGT_OMEGA_GPU_DEVICE_ID` 为一张物理 GPU 的 UUID，配置
   `VGGT_OMEGA_CHECKPOINTS_DIR` 为包含 `vggt_omega_1b_512.pt` 的目录。
   权重只读挂载，镜像不会下载权重或接收 Hugging Face token。
3. 按需要配置 `VGGT_OMEGA_AUTH_TOKEN`。容器只绑定宿主 loopback 的 8006 端口；
   跨主机部署使用现有受控网络和 registry 认证配置。
4. 构建并启动可选服务：

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-vggt-omega build vggt-omega-backend
   docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-vggt-omega up -d --no-deps vggt-omega-backend
   ```

5. 在模型市场注册服务，鉴权方式选择 Token（API 字段 `auth_method=token`，使用 Bearer 请求头），
   检查能力，为项目启用服务池并配置实际 GPU claim。
   容器 `/setup` 中 `point_cloud_densification` 表示生成能力，`models=[]` 表示
   它不提供预标注模型。未通过当前部署的实卡生命周期验收时，保持
   `VGGT_OMEGA_MANAGED_LIFECYCLE_VERIFIED=0`，不能宣布 enforce 已就绪。
   项目「ML 模型」中可以启用该服务，主后端选择会排除这种仅生成辅助资产的服务；
   补强生成配置在「工作台规范」中设置。
6. 项目设置中开启实验，选择生成服务和场景。后台单个任务最多处理 500 个点云帧，
   每帧使用六路当前相机，执行形状固定为 B6S1。

基础镜像、Python 依赖、模型源码 commit 和权重 SHA-256 都固定。
完整协议和所有容器变量见仓库中的 `apps/vggt-omega-backend/README.md`。
上游附带 FAIR 非商用许可证；取得下载权限并不改变该许可范围。

## 输入与发布

相机必须与主点云属于同一帧，并有可信 SHA-256、尺寸和正确逐帧标定。
后台生成使用 `omega-current-mono-512-full-lidar`，完整当前实测回波用于
尺度、局部深度校准和保护已有测量射线。该结果不附带独立留出精度结论。

任务先创建可见的 AsyncJob，再入队。每帧预留 generation 租约，深度和置信度
通过签名地址上传到临时 import 桶；平台核验字节、摘要、类型、尺寸、
预处理矩阵及模型身份后，才执行 CPU 几何处理。发布事务再次检查权限、取消请求、
源文件和标定，成功后切换 active 指针。失败帧可重试，有效结果直接复用。
冻结输入采用稳定幂等键，容器 CPU 缓存仍有效时，重试可向新签名地址重传深度和置信度。
中断任务超过两小时无更新时由后台对账标记失败；临时对象有即时删除和桶生命周期兜底。

关闭项目或部署开关即可停止新生成和辅助资产访问。已保存的标注不受影响。

## 导入已验证的离线结果

离线对照使用 `omega-current-mono-512`，与完整 LiDAR 生成 profile 分开。
准备 JSON 映射 `[{"task_id":"UUID","frame":38}]`，其中 frame 是离线 bundle 的帧号。
平台会核对当前 PCD 行、原 RGB、源 token、模型身份和逐曝光标定；文件名相同不代表可复用。

清理后的历史 bundle 保留单图预测、逐相机点云、协议、构建记录及原始
`current-manifest.json`，无需离线预览或旧构建脚本。使用该清单的原 SHA-256；
原 RGB/LiDAR 必须仍在协议绑定的数据集路径。历史清单也列出已清理的试验文件，
导入只校验所需子集，不能将导入成功解释为完整历史 bundle 仍然存在。

在正确的运行环境中执行，先检查再发布：

```bash
cd apps/api
.venv/bin/python scripts/publish_point_cloud_auxiliary.py --bundle /path/to/current-frame --manifest-sha256 MANIFEST_SHA256 --mapping /path/to/mapping.json --actor-id MANAGER_UUID --check
.venv/bin/python scripts/publish_point_cloud_auxiliary.py --bundle /path/to/current-frame --manifest-sha256 MANIFEST_SHA256 --mapping /path/to/mapping.json --actor-id MANAGER_UUID
```

## 旧 nuScenes 标定修复

早期导入可能把首帧外参广播到整个 Scene。读取辅助资产时不会自动修改标定，
而是报告 stale。修复分两步，清单应先由管理员检查：

```bash
cd apps/api
.venv/bin/python scripts/repair_nuscenes_calibration.py --task-id TASK_UUID --actor-id MANAGER_UUID --snapshot /path/to/repair.json
.venv/bin/python scripts/repair_nuscenes_calibration.py --task-id TASK_UUID --actor-id MANAGER_UUID --snapshot /path/to/repair.json --apply
```

应用会核对源摘要和标定版本，只修复与导入基线一致的值，并追加标定历史。
人工编辑、缺少基线或清单已过期时拒绝覆盖。新导入直接使用逐曝光位姿。

## 资产对账

默认仅报告。确认输出后使用 `--apply`；只处理专用辅助资产前缀，
不会删除原 RGB、PCD、标注或离线实验目录。

```bash
cd apps/api
.venv/bin/python scripts/reconcile_point_cloud_auxiliary.py --limit 100
.venv/bin/python scripts/reconcile_point_cloud_auxiliary.py --limit 100 --grace-hours 24 --apply
```

存在 `next_cursor` 时用 `--cursor` 继续分页。活跃租约和仍被引用的 generation 会保留。
