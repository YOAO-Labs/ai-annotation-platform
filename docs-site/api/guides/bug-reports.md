---
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-24
---

# BUG 反馈与截图附件

基路径为 `/api/v1/bug_reports`。提交、上传和下载均要求 `Authorization: Bearer <token>`，沿用账号和 API Key 的现有权限，不增加 scope。提交者可查看自己的反馈；项目管理员和超级管理员可查看反馈列表与详情。

## 上传并提交

1. 对每张图片调用 `POST /screenshot/upload?file_name=<URL编码文件名>`，请求体直接放原图片字节，`Content-Type` 为 `image/png`、`image/jpeg` 或 `image/webp`。该入口不接收 multipart，也不返回对象存储上传 URL。
2. 保存响应中的实际回执：`{ "storageKey": "bug-report-attachments/<账号UUID>/<上传UUID>.png", "fileName": "截图.png", "mimeType": "image/png", "size": 1234 }`。
3. 调用 `POST /bug_reports`，提交 `title`、`description` 和最多五个回执组成的 `attachments`。附件必须由当前账号上传、已经核验、仍在保存期且尚未绑定反馈；键不可重复，文件名、MIME 和大小必须与上传回执一致。

单张图片最多 10 MiB、4000 万像素。服务端检查真实图片格式和完整性，保留原始字节；文件名最多 255 字符，拒绝路径分隔符与控制字符。成功上传按创建时刻固定保存 180 天，提交反馈和重复下载不会延长。上传失败不提供可用回执，请重新上传；不确定写入保留独立责任记录，后台不会按年龄删除未确认的上传。

兼容字段 `screenshot_url` 只能是首个附件的相同键；省略 `attachments` 时，可以使用本人已核验且未绑定的键，服务端展开为一个完整附件。任意 URL、未知旧键与他人上传均不能用于新反馈。一个回执只能首次绑定一条反馈，删除反馈后也不能复用。

旧 `POST /screenshot/upload-init` 已标记 deprecated。合法请求返回 `409`，`detail.code=bug_attachment_upload_init_retired`，并在 `detail.upload_path` 提示新入口；非法 MIME 仍返回 `422`。旧客户端需更新为上述认证字节上传。

## 下载附件

调用 `GET /{report_id}/attachments/download?key=<URL编码附件键>`，携带 Bearer Token。只有当前可见反馈中登记且与独立回执绑定的准确键可以读取。

成功返回 `200` 和已完整核验的图片字节，`Content-Type` 为原图片 MIME，`Content-Disposition: attachment` 使用安全文件名，缓存策略为 `private, no-store`。该入口不再返回 `302` 或 MinIO URL。浏览器应使用认证 fetch 获取 Blob，再创建临时 object URL 下载，完成后及时释放。

| 状态码 | 含义                                              |
| ------ | ------------------------------------------------- |
| `400`  | 该键没有登记在当前反馈中                          |
| `403`  | 账号或原 API Key 已失效，或当前不可见该反馈       |
| `404`  | 反馈或已保存的对象不存在                          |
| `409`  | 未知旧归属、回执/绑定不符、文件损坏或维护状态变化 |
| `410`  | 附件已超过固定保存期限                            |
| `413`  | 上传请求实际字节超过 10 MiB                       |
| `422`  | 请求字段、MIME、文件名或图片内容不合法            |
| `503`  | 存储暂不可用或无法确认物理读取已收尾              |

错误使用 `{"detail":{"code":"固定错误分类"}}`。上传和下载会在外部 I/O 后再次检查原账号、原 API Key、反馈关系和存储维护状态；因此中途撤权或关系变化可能使操作失败，即使存储 I/O 已完成。

## 删除与历史记录

`DELETE /{report_id}` 先在同一事务中退休已知附件并删除反馈与评论，提交后尝试清理文件；清理失败保留责任记录，由 cleanup 队列每日有界重试。未结束的同附件读取会推迟删除，过期附件也由这套流程清理。

旧反馈的附件元数据仍可查看，但缺少独立回执的文件不会从当前桶猜测读取或删除。需要文件时重新上传；旧无归属对象保留待核对。
