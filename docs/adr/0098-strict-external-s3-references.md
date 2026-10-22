# 0098 — 外部 S3 引用保存精确定位并逐次验证条件读取

- **Status:** Accepted
- **Date:** 2026-10-23
- **Deciders:** 用户授权推进完整 E03；实现与只读复核
- **Supersedes:** —

## 背景

旧连接器导入会规范化对象路径并将原图复制到平台，不能承载长期外部引用。对象 key 可包含连续斜线、点段、空格或反斜杠，按文件系统路径重建可能读到另一对象；ETag 也不是内容摘要。远端分页列表无法证明全局同一时刻的完整树。

## 决策

复用现有 Source、ScanEntry、Registration、Reference、Asset 和 Task 内容身份。新增严格 S3 后端，旧复制导入适配器保持原调用者。精确 bucket/key 与观测保存在扫描条目和不可变内容回执中，不写入 Item.file_path 或伪装为平台原图副本。

```text
UI -> Source/Scan/Registration -> S3Access -> external S3
Task -> verified snapshot -> Workbench / annotation guard
```

LIST 显式请求 url 编码并严格解码一次，ContinuationToken 原样保存。根 ScanEntry 的 observed.checkpoint 保存分页进度；页内容与下一 token 同租约事务提交，取消保留已提交页，循环或缺失 token 失败关闭。S3 不使用本地 inode，也不宣称多页 LIST 是原子快照。内容身份由后续实读 SHA 建立。

首次 HEAD 核对扫描观测，条件 GET 固定可用 VersionId，否则使用原始 ETag；响应必须是完整200、长度及返回身份一致，实读完整 SHA 后才交付快照。每次读通过错误 IfMatch 被拒绝验证服务的条件合同，不能由仅可列举的根探测代替。版本存在时保留旧版本；版本缺失或无版本对象变化时拒绝，不自动回退最新字节。LIST 时间与 HTTP 日期统一为 UTC 秒精度，时间仅为观测。

凭据沿用连接器加密存储，仅在线程内创建客户端时解密。SDK 使用内建条件、版本与超时，关闭代理并在最终发送时限制到配置 endpoint。管理与委托任务读取继续使用各自现有权限；I/O 前后复查账号、Key、位置/连接修订和白名单。保持 Connection→Location 更新锁序，Location 锁内对 Connection 仅作 MVCC 读取。平台固定位置保持不可修改且不作为外部来源。

## 后果与边界

- 原地登记不写平台原图；派生预览按既有独立策略计量。托管复制使用安全内部名称，精确来源键仍由 Source/ScanEntry 固定。
- 每次读取会增加条件资格请求与完整校验 I/O，仍受快照预算约束。来源整体故障停止尝试并保留剩余候选，单对象错误独立记录。
- 目标限制不等于 DNS IP 固定，SDK 再解析的既有风险保留。兼容服务可自行拒绝部分合法 S3 key；平台不会静默改写。
- 回退关闭新建外部来源而保留引用与账本。SFTP 持续引用需要明确服务器隔离资格，单独交付；周期同步和冻结另属对应 Epic。
