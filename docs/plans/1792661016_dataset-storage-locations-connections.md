# EPIC-02：存储位置、连接配置与业务用途绑定

> 状态：E02-A、B1–B12 与 B13 各切片至 B3-J completed locally。完整 E02 继续执行，完整用途切换关闭。用户已授权持续推进和提交，不分配发布版本。
> 转入日期：2026-10-22；本地核验基线：`e169d9ef53a9eda60abd4e67bf553ab4e6ab618e`。
> 实施前须通过 [backlog promotion gate](backlog/README.md#promotion-gate)。[草案总览](backlog/1792661016_dataset-storage-roadmap.md)、[架构契约](backlog/1792661016_dataset-storage-architecture.md)和[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md)共同约束本系列。

> 优先级：P0（M1 基础）；负责人角色：后端、运维，前端协作。
> 依赖：EPIC-01 的标识与适配契约；EPIC-11 的最小权限规则必须随 M1 一起交付。
> 下游：EPIC-03 原地注册、EPIC-05 读取、EPIC-07 迁移、EPIC-09 存储控制台。

## 已实施定案：E02-B9 审计归档固定归属与源分区事务保护

基于干净HEAD `d65949ea4` 延续整套实施/提交授权，使用think工作流核对既有决策及真实调用，不另设批准轮次。当前归档只从当前桶按固定月键上传，PUT后直接DROP；缺独立回执、完整回读、真实源OID/整月边界与并发INSERT保护。Worker running/失败尚未独立提交，上传失败被吞掉；通用取消未与DROP串行。源码、角色脚本及PostgreSQL16官方LOCK/分区维护规则已核对；本片按完整归档消费者交付，不把改桶或空分区成功当成安全归档。

0226增加独立AuditArchiveOutput：UUID、Job UUID快照（无FK）、year/month、public源schema/table、父/子OID历史快照、UTC自然月起止日期、格式修订1、源行数/规范JSONL字节数/源SHA、压缩SHA/大小、严格audit-archive存储引用、created_at、pending/ready和一次published_at。精确候选键 `{year}/{month:02d}/{uuid}.jsonl.gz`，每次尝试独立且条件创建；同月最多一个ready，遇已归档月的人为重建源分区保留在线并报冲突，不覆盖或选择最新代际。所有身份/内容/时间事实不可变，状态只pending→ready；无账号或源表依赖，不能删除归属行。复用受限restore标记，仅允许表owner在恢复事务中映射bucket/identity；有事实时拒绝破坏性downgrade。源OID只保留历史，恢复不改成目标OID，pending不自动继续DROP。

源只接受public.audit_logs的普通直接叶分区、规范 `audit_logs_yYYYYmMM` 名称及完整UTC月边界，父表必须按created_at范围分区；拒绝默认/子分区、外表、其他schema同名和不符附件/OID。retain_months必须为正整数，不用0/负数触发清空；候选只取完整结束且保留期外月份。ensure_future同样核对真正附件、OID与完整边界；同名独立表不被IF NOT EXISTS冒认。所有动态标识符由严格规范化身份生成、限定schema并quote。

初次事务明确READ COMMITTED，先取得月份xact advisory互斥，仅对目标child取SHARE NOWAIT，获锁后重核catalog，按 `(id,created_at)` 服务器流式游标生成确定性gzip（mtime=0），规范JSONL逐行计数/摘要并写唯一末行 `_partition_meta`。不整月载入内存；受600秒核验预算、每行16MiB、源256GiB/一千万行、压缩64GiB约束，超限保留源并明确失败。meta记录格式、月份和源身份、行数及源内容回执；读取API沿用meta作为末条普通dict，total为源行数加1。先提交pending与所有独立回执并释放源锁，才在durable storage permit中条件PUT、条件完整回读压缩SHA和完整解压语义；所有句柄与线程真实收尾，临时文件由本次尝试关闭。失败、取消、关闭/提交应答未知不补偿删除，不按年龄回收pending。

最终新READ COMMITTED事务取得同月xact advisory、Job及owner锁，确认当前尝试仍running且未取消，锁child SHARE NOWAIT并复核同一父/子OID、附件/边界，重新流式计算完整源回执。内容变化拒绝DROP并保留源和候选。全部本地重算通过后才尝试 `LOCK TABLE ONLY public.audit_logs IN ACCESS EXCLUSIVE MODE NOWAIT` 和child ACCESS EXCLUSIVE NOWAIT；不能等待锁升级，避免已持父表ROW EXCLUSIVE且等待child的routed INSERT形成死锁。重查catalog、pending回执、维护Gate及取消，限定schema DROP；同事务写ready、published_at、AUDIT_ARCHIVE审计和Job已提交月份/计数。父表锁不跨S3 I/O；最终锁冲突回滚并保留源。拒绝复用旧REPEATABLE READ/SERIALIZABLE snapshot；初次/最终新事务在任何SELECT前确定隔离级别。最终commit应答未知不以源缺失猜成功、不删候选；只认持久ready事实，未知pending不重放。

Worker显式耐久create/running；按每月成功事务保存进度/结果，失败如实保存failed及固定错误码并保留已发布月，绝不吞错误冒充completed。中途取消在各安全边界检查，cancel接口对audit_archive使用与最终DROP相同Job锁；先赢则禁止后续DROP，DROP先赢则保留该月、取消剩余。已终态不得被迟到结果或signal覆盖；原signals锁和无user_id的系统Job语义复用，不新增用户通知。其他track_job调用不改。

两个既有GET保留URI、分页参数、DTO形状及super_admin权限，不增加scope。list只查ready归属，不扫描当前桶；无已登记owner的月份返回结构化409明确历史未知，已知缺失404、完整性409、临时存储/关闭未知503。read在I/O前后锁原账号/原Key hash/scopes/expiry和当前super_admin角色，按UUID许可完整条件读保存桶、关闭句柄并校验全部压缩和解压内容，只保留请求分页窗口；不能先slice跳过尾部损坏，不返回MinIO地址。当前审计页没有归档UI，本片不新增页面。归档没有自动过期/物理清理，未知旧月对象保留待独立清单接管；本片不提供猜测接管工具。

本片超过8文件：隔离树一负责模型/0226、严格存储/确定性codec与测试；隔离树二负责catalog/两次快照/最终DDL流程及并发回归；最终另用第三棵隔离树负责认证读取/API与对应测试和指南；主树负责Worker/取消、集成回归、恢复夹具/映射、maintenance实测夹具和正式文档/派生合同与真实验收。最小替代是在固定月键上保存bucket并立即DROP，但无法证明源快照、上传字节和并发写入一致，因此保留独立候选。最脆弱前提是物理传输和最终commit是否确认结束，设计通过永久保留未知pending和不自动重放承受失败。新增服务内部模块不新增进程/依赖/环境变量/外部账号或凭据；沿用所属DB/MinIO/Redis及维护连接，不改共享配置、镜像或版本。

```text
已验证源身份 -> child SHARE / 流式JSONL+gzip -> pending回执提交并释锁
                                                       |
                             durable permit -> 条件PUT + 完整回读/解压
                                                       |
新事务 -> 月份/Job/owner锁 -> child SHARE / 全源重算 -> ONLY parent NOWAIT
                                                       |
                       child NOWAIT + catalog/Gate/取消复核 -> DROP + ready + audit + progress提交
API -> 原super_admin/Key -> UUID读取许可/保存桶完整核验 -> 当前授权/同owner/Gate -> 有界分页
恢复 -> 仅bucket/identity映射，源OID/内容/状态/已删除源与未知历史均保持
```

验证以已确认owner的一次性test命名空间为唯一DB目标：严格schema/0226/restore/禁止删除、确定性三态内容和空分区、相同id不同created_at、同键异桶/损坏/缺失/压缩炸弹/尾部坏行/句柄关闭；旧未知零I/O、角色/原Key两窗变化及自然到期；真实数据库并发旧月INSERT、当前月不受网络阻塞、父路由INSERT与NOWAIT失败、rename/detach/同名换OID/错误边界、两Worker交错、read isolation；pending提交失败零PUT、上传失败零DROP、最终DDL/ready/audit原子回滚、未知commit与取消各顺序、持久failed和迟到终态。真实HTTP/MinIO/当前maintenance Worker归档非空旧月并经API完整读回、缺失/损坏拒绝、取消/失败和源销毁后的离线恢复。源/目标随机拥有资源、ready已DROP源/pending仍有源及旧无owner字节均恢复，原OID不改。同步README、CHANGELOG、API/async-jobs、用户审计、架构和部署恢复文档；Ruff、OpenAPI/codegen、SDK合同、文档生成/build、正常hooks和diff通过后记录Outcome。无样式变更，不重复完整主题矩阵；如浏览器能展示现有Job/接口上下文则验收现有流程，不虚构归档UI。保留人工3100/8100、两Worker/Beat及原24对象/12文件字节，迁移/刷新仅所属环境。全部测试资源/产物按owner清理；完整E02及用途切换继续active/关闭。LAN脚本给runtime审计DDL而基础/生产维护用owner，部署按实际权限核查，本片不声称生产独占、Object Lock、RPO/RTO或规模资格。

## 本轮实施定案：E02-B8 报告附件固定归属与认证字节传输

基于干净HEAD `547fe6f05` 延续整套实施和提交授权。只读核对确认截图直传当前bug桶、无独立回执且URL可重放；报告只按前缀接受key，旧screenshot_url还允许任意字符串；两个Web下载入口用普通链接，未携带HTTPBearer。审计归档另有PUT后直接DROP分区、缺源快照围栏和固定归属的完整缺口，记录为下一独立消费者阶段，本片不借改选桶声称它已完成。

0225增加独立BugAttachmentUpload：随机UUID、上传者UUID快照（无账号FK）、严格bug-reports存储引用、实际SHA/大小/MIME/安全文件名、固定created/expires（180天）、pending/ready/retired及退休/清理时间、可空且只可首次写入的bound_report_id快照（无报告FK）。精确键 `bug-report-attachments/{owner}/{id}.{png|jpg|webp}`；身份/回执/期限不可变，状态向前，绑定只在ready首次发生。复用现有表owner及恢复事务标记，仅允许嵌套bucket/identity映射；有事实时拒绝破坏性降级。

新增认证 `POST /bug_reports/screenshot/upload?file_name=...`：raw PNG/JPEG/WebP，最多10MiB，服务端核对真实格式/40M像素预算，保留原图字节；返回现有BugAttachment四字段别名对象。先捕获请求原账号/Key hash/scopes/期限并释放锁，再有界读请求和校验；创建独立pending与固定回执并提交，才在durable permit内PUT并条件完整SHA回读。真实收尾后复核原账号、原Key、Output快照及Gate修订才ready；失败/取消/commit应答未知不补偿删除，pending不按年龄reap。旧upload-init标deprecated并对合法请求返回409及新路由提示，不再发可重放最终对象PUT；旧客户端须更新，不新增服务、凭据、依赖或环境变量。客户端单次上传仍是同一截图/粘贴/附件产品操作。

报告创建最多5个附件，同事务锁定并绑定本人ready、未过期、未被其他报告消费的owner，元数据使用实际服务端回执。拒绝未知key、他人owner、重复消费/重复key、声明MIME/大小不符；screenshot_url仅兼容同一首附件的别名，screenshot-only合法已知owner可展开成一个附件，旧未知引用明确409，不猜当前桶。report_id在报告创建后同事务首次固定。报告删除同事务退休其已知owner，再删除报告/评论，提交后执行耐久清理；旧未知引用不物理删除。已有报告元数据/历史保留，NULL旧附件需重新上传，原文件保留待核对。

原附件download URI改为带Bearer的受控字节响应（200 image attachment，替代302签名跳转）：按报告可见性、已登记准确key及绑定owner准入，在含owner UUID的read purpose下完整条件读取保存桶并关闭句柄，最终重新锁账号/原Key、当前报告可见性/登记关系、同一ready回执、期限及Gate，再返回已核验有界字节。缺失/损坏/未知位置/过期明确区分；不返回或续签MinIO URL。前端统一认证fetch blob、临时object URL下载并及时revoke，不向存储传token；错误和进行中状态在当前附件旁可见。

Web共享下载hook绑定账号/会话/当前报告生命周期，Drawer提交在上传前捕获账号/token/会话并在每个await后与create前核对，换账号、关闭/重新打开或当前详情切换后旧流程不能提交、下载或修改新界面。保持五图、粘贴、编辑、上传失败重试/跳过、管理详情、评论、居中详情和原返回/分页；只改必要按钮语义和状态，使用现有字号、样式tokens与组件。

cleanup周期任务分批处理已过期ready及retired，锁owner并排除同owner未收尾read permit后提交retired；退休后拒绝新读取。按保存桶精确删除，最终重核快照/状态和当前关联后才删行，失败保留。报告仍存在且未过期的关联阻止删除；到期后的元数据可保留但不再提供文件。移除应用为bug桶设置的整桶180天lifecycle，避免绕过读permit；按owner固定期限治理，其他用途规则不动。部署先暂停附件写入，盘点旧直传窗口并等待实际收尾，迁移/刷新所属API、cleanup Worker与Beat，确认bug桶原整桶规则已移除后恢复。旧无owner文件不批量清除。

本片超过8文件：当前HEAD隔离树一负责领域/0225/严格存储及专项测试，隔离树二负责上传/绑定/认证读取/删除与Worker/路由及回归，隔离树三负责Web上传/共享下载/提交会话围栏、相应交互测试与用户指南；主树负责集成、恢复白名单/夹具、部署文档、API artifacts和实际验收。最小替代是只在报告JSON加bucket，但不能承接上传前/失败责任、报告删除后的清理，也无法阻止URL覆盖与跨账号消费。最脆弱前提是旧直传请求是否结束，设计通过不再签发旧键、独立新键和保留未知历史承受其失败。

验证包括三种实际图片、严格schema/迁移/restore、原Key和账号两窗口变化、零I/O拒绝/同键异桶/同大小损坏、报告一次性绑定/别名/删除、上传及读取取消真实收尾、未知close/commit与清理竞争、固定期限和旧无owner。DB仅用验证owner的一次性test；真实HTTP上传/创建/下载、Celery清理和源销毁后离线恢复三态/关联/字节/期限。Web测试覆盖上传失败、换账号/close和下载/URL释放；实际浏览器明暗、桌面/窄屏及控件/错误/溢出，截图供检查后清理。Ruff、OpenAPI/codegen/Web类型、Web lint/CSS tokens、SDK合同、文档build、diff及正常hooks通过后提交Outcome。保留人工3100/8100、两类Worker/Beat、24对象/12原件字节，全部本轮资源按owner清理。回退应用保留独立owner/绑定/期限，暂停新附件即可，不能恢复按当前桶猜读。

## 本轮实施定案：E02-B7 异步导出候选、缓存与到期清理

基于干净HEAD `767625498` 继续已有整套实施/提交授权。当前ExportArtifact只保存缓存索引，Worker以当前export桶上传、HEAD探活和签名，失败后直接删除候选；相同Job重投递复用键，缓存命中还会新签七天但文件仍按原保存期过期。本片完整固定异步导出候选的物理责任，并保留既有ZIP内容、缓存指纹及公开Job/通知格式，不开放用途切换。

0224新增独立ExportOutput：随机UUID、项目/Job UUID快照（无FK，避免源记录清理丢失物理责任）、image/video媒体段、format/file_count、SHA/字节数、严格export storage_ref、固定created/expires及pending/ready/retired状态/退休/清理时间。每候选使用 `{media}/{project}/{job}/{output}.zip`，同Job重试不覆写旧候选。内容及身份/期限不可变，状态只向前；复用现有受限恢复标记和表owner规则仅改bucket/identity。有任何Output或缓存关联时拒绝破坏性降级。ExportArtifact增可空output_id RESTRICT外键，作为可替换缓存索引，物理责任留在独立Output。

生成ZIP后有界流式取得独立本地SHA/大小，再分配并提交pending，才在durable permit中上传并完整条件有界回读。期限在分配时固定七天，上传消耗该窗口，不因缓存复用延期。64GiB输出/600秒协作读取预算、分块摘要、不将整包留在内存。全部I/O异常、取消或发布commit结果未知均不删除候选，pending保留待人工确认真实停写和独立回执；不按年龄自动reap。最终原有Job取消、账号/项目/任务/引用来源围栏不变，另复核Output快照、维护修订，共同提交ready、缓存和结果/通知。原件读取、ZIP内容合同与选中范围不因物理归属变化重写。

三个新建入口（项目、批次、选中任务）均另保存不公开的output_authority，固定请求原账号/项目及API Key hash/scopes/expiry；不引入新scope要求。规范请求原token/认证scope在捕获时验证，Worker首次执行和最终签URL前重新验证，最终持Key共享锁到提交。引用路径原export_authority继续独立负责任务/来源内容，不把所有托管导出伪装成引用。历史未保存输出授权的未完成Job不新建产物，应重新发起导出；已完成旧URL继续原样展示，不从当前桶重签。

缓存lookup只从已关联ready Output的保存桶完整核对实际SHA/大小，校验Artifact的project/key/size/file_count/format/deadline与Output一致。NULL旧索引零物理I/O视为miss；已知对象缺失或字节损坏只失效索引并重新构建，不提前删旧Output/字节；未知身份/端点冲突明确拒绝。签名前锁定并复核同一已核验快照、所属项目、ready/期限及维护代次，签名align=False且只给固定期限剩余秒数。record安全替换NULL/过期索引，竞争中的其他未过期有效索引保留，避免全表唯一cache_key导致过期重导冲突；每个Job仍可发布自己的独立输出。

过期清理分批锁Output；确认期限已过、无同Output的durable读取permit后，删除关联缓存索引并提交retired。读permit采用含Output UUID的purpose，on_acquired短事务复核ready/期限/快照后释放业务锁再物理读，防止退休与开始读取竞态；不确定关闭保留permit，清理继续延期。退休行提交后按保存桶精确删除，再复核状态/快照/引用后删行；每项失败保留并重试。旧NULL索引仅过期删除数据库行，绝不猜物理键。Job/Project先消失不改变Output快照、已签期限或清理责任，后台不自动恢复/重派pending。

本片超过8文件：领域/0224/严格存储与流式I/O及专项测试由一棵当前HEAD隔离树负责；Output工作流/缓存/导出与清理Worker及其回归由另一棵隔离树负责；主树负责三个API入口的输出授权、DTO隐藏、恢复白名单/合成夹具、旧测试适配、正式文档和实际环境验收。最小替代是在缓存行加bucket，但会丢失写前/失败候选责任、项目级联删除后的责任并允许重试覆写，因此保留独立物理Output、复用现有缓存索引。最脆弱前提是旧线程是否真正停止，设计用每候选独立键和永久保留未知pending承受其失败。不新增依赖、服务、环境变量、凭据或镜像。

```text
三个API入口 -> 原scope/内容证明 + 隐藏output_authority -> 导出Job
                                                               |
冻结ZIP -> SHA/size -> 独立pending(ref/key/deadline)提交 -> permit PUT + fullGET
                                                               |
                         当前授权/取消/Gate/Output复核 -> ready + cache + result
                                                               |
新Job -> cache -> UUID读permit + 保存桶SHA核验 -> 同一Output剩余期限签名
                                                               |
期限已过 + 无读permit -> cache移除 + retired提交 -> 保存桶精确删除/失败重试
离线恢复 -> 仅bucket/identity，内容/期限/状态/Job与Project快照保持
```

验证覆盖所有格式共用Worker、全项目/批次/选中/引用范围、跨桶同键陷阱、NULL旧缓存零I/O、已知丢失/损坏重建、过期索引替换/竞争、期限cap、重复投递独立键、PUT/commit结果未知/取消真实收尾、账号/Key撤权、维护与读permit退休竞态、Job/Project清理、严格迁移/受限恢复。DB仅使用确认owner的一次性test环境；真实HTTP+MinIO+Celery从新Job建立ZIP并完整下载，验证缓存、失败保留和过期清理；浏览器观察现有任务下载链路，不将合成夹具称为生产或原生GPU资格。Ruff、OpenAPI/SDK、生成类型、文档和diff/正常hooks通过后提交。迁移并刷新所属人工3100/8100、两类Worker和Beat，核对既有业务及24对象/12原件字节；清理全部本轮测试资源/中间产物。回退保留归属表、引用和期限，关闭新导出即可，不恢复当前桶猜测。

## 本轮实施定案：E02-B6 上传头像的固定归属与耐久清理

基于干净本地HEAD `5aad08281` 继续已有整套实施和提交授权。只读盘点确认其余八用途仍有当前桶消费者，且全用途legacy binding校验尚不允许切换。本片选择单一规范化写入口的头像；基础权重由管理员准备脚本按显式桶发布/下载至节点缓存，训练派发不临时下载基础权重，不为其凭空引入训练对象表。

0223新增独立AvatarUpload记录，以原32位随机token为主键，保存创建账号UUID快照、规范化WebP的SHA256/字节数、严格非秘密storage_ref、pending/ready/retired状态及创建/退休/最后清理时间。归属固定avatars用途、legacy位置/修订/代次、真实端点、桶、精确token.webp键和存储身份；身份及内容回执不可变，状态只向前推进。账号快照不授予权限，也不随账号清理丢失对象清理责任。受限离线恢复只允许所属恢复角色在事务标记下改bucket/identity；存在记录则拒绝破坏性降级。

上传先持久pending归属与独立规范化摘要，再通过已有durable storage permit执行PUT及完整回读核验；真实I/O收尾后重新锁定账号、复核账号/Key原证明、头像引用CAS和维护修订，同事务发布ready新引用并将旧已知对象标记retired。并发选择或撤权不覆盖新状态；提交失败/应答未知不做候选物理删除，pending留待人工对账，不按时间自动回收，以免进程崩溃后迟到PUT与删除竞争。已确认失败且线程真正结束可以退休；不确定收尾继续保留。选择内置或清除同样锁定账号、提交后才尝试退休对象清理。

匿名token图片URL、avatar_ref与UserOut保持原格式。GET只查独立ready记录，按保存桶和精确键有界读取并核对完整SHA/大小后返回WebP及内容ETag；错误不回吐底层存储异常。端点/身份变化先于I/O拒绝。无归属的历史token不从当前桶猜测读取或删除，用户可重新上传或选择内置头像；不从缺乏独立摘要的旧文件名自动接管。旧未知对象保留供核对，GET不写记录。此边界在用户指南和升级说明明确记录。

退休行本身是耐久清理意图；提交后尽力清理，失败保留供cleanup队列周期重试。清理只处理retired、无任何用户avatar_ref引用的记录，先提交业务锁，再使用保存端点/桶与精确键删除；重读条件后移除记录。pending绝不由后台自动物理删除。每项失败不阻断其他项，维护排空阻止新物理操作。继续使用现有storage_access真实线程收尾，无新依赖、服务、凭据、环境或镜像。

本片超过8文件：领域/模型注册/0223及迁移和纯归属测试由当前HEAD隔离树负责；服务/上传与匿名读取路由/cleanup Worker及针对性测试由另一隔离树负责；主树负责集成、恢复白名单和测试、正式文档、真实一次性环境及人工环境刷新。最小替代是在User上加bucket，但引用替换会丢失旧对象和失败上传的清理责任，故选择独立token记录；不扩展成通用对象注册中心。最脆弱前提是进程退出后物理写入是否结束，设计通过保留未确认pending事实承受该前提失败。

```text
规范化上传 -> pending(token/ref/SHA)提交 -> durable permit PUT + 完整回读
                      |                              |
                      |                  账号/Key/引用CAS/Gate复核
                      |                              v
                 未知收尾保留               ready + User引用 + 旧retired提交
                                                     |              |
匿名GET -> ready独立记录 -> 保存桶精确键/SHA核验       |       cleanup退休意图
                                                     +------- 同端点删除
离线恢复 -> 受限bucket/identity映射，其他回执/业务事实不变
```

验证覆盖新上传/匿名读取/缓存、清除/内置替换、同键不同桶、端点和摘要错误零误读、旧未知记录零I/O、并发头像选择、撤权/Key变化、PUT和发布失败/应答不确定、取消真实收尾、pending保留、失败清理重试、维护Gate、严格迁移/不可变/受限恢复。全部DB测试仅在验证owner的一次性test环境运行；真实HTTP/MinIO与当前Worker执行定向验收，浏览器检查实际头像字节呈现，不因后端变更重复完整UI主题矩阵。正常hooks、Ruff、API/SDK合同、文档构建和diff检查通过后提交。人工3100/8100、两类Worker/beat及既有业务和全部原件/冻结字节保留；回退保留表与归属事实，可关闭新上传，不恢复全局桶猜测。整用途切换及其余用途继续后续实施。

## 本轮实施定案：E02-B5 预览、样本评估与主动学习输出归属

在干净基线 `2d49fa56e` 继续整套实施授权。剩余training用途消费者分别持有三类独立输出：TrainingPreview的 `projects/{project}/previews/{preview}/` 图片和报告，SampleEvaluationRun的 `projects/{project}/sample-evaluations/{evaluation}/` 各attempt报告，以及ActiveLearningRound的 `projects/{project}/active-learning/{round}/media/` 跨attempt复用候选图片。本片整体固定这三个根前缀；输入训练版本、来源模型和候选原件继续由已有独立所有者负责。

0222为三个现有记录增加可空内部storage_ref，沿用0220/0221的严格envelope与不可变/受限恢复触发器，purpose分别为training-preview、sample-evaluation、active-learning。复用已实现的共享分配/候选/端点和键检查，薄适配器保持三类键形状；只有显式新建入口分配，new_attempt、重放和重试不得默默为旧NULL记录猜测。原冻结spec、上传/推理wire、内容摘要及模型/人工证据不变，不新增服务、配置、凭据、依赖或镜像。

全部输出签名、上传、回读、报告缓存命中前核验、候选PNG读取/复用和临时报告候选清理使用保存桶并严格检查所属前缀/attempt或ordinal。NULL及端点/身份变化在物理I/O前返回明确冲突；旧记录仍可查看元数据、取消或按原围栏确认失败/退出。主动学习遇到未知归属不得先读原件重造候选；重试不改变固定派生SHA或原推理请求/预算身份。通用JSON读取/内部下载的默认桶只保留给仍明确未转换的用途，本片三个所有者不使用默认值。

历史仅通过项目负责人显式storage-pin接管当前保留副本，分别置于原三类资源下。记录及当前attempt须终态、无保留CPU租约/活动claim，预览须等已有签名PUT窗口关闭。旧attempt须终态，或为非current、更旧generation、claim已清空且completed_at已保存的封存lost；评估正常终态保留的claim不是活动claim。这只证明DB发布围栏，不能声称旧物理线程已关闭。候选只读持久binding；核验全部前缀，按独立发布/上传清单或冻结候选派生SHA检查实际完整字节、大小（有独立记录时）、metadata及已保存语义，未知额外键、已发布缺失、变化均拒绝。旧评估报告用独立发布回执核验身份，不假定跨重试共享Sample行仍保存每次旧报告的执行证据；当前报告继续完整语义复核。失败/取消的未发布对象可缺失，存在仍须核验；主动学习已持久media/成功候选要求其PNG存在，其余已知成员的残留PNG按冻结SHA核验。只从保留副本验证，不读来源补证或修复，也不反序列化权重。沿用Reader、共享进程容量、维护permit、真实关闭及发布前权限/Key、记录/attempt/候选、binding/Gate修订复核。

保留现有耐久清理责任：Preview行在进程退出与PUT窗口关闭后才可过期清理；SampleEvaluationRun行保留deletion_requested_at直到保存前缀删除完成，并继续检查主动学习保留引用。未知归属保留行并明确拒绝物理删除，后台逐项失败不阻断其他合格项。主动学习本来没有删除入口，本片不新增。离线恢复把三个归属纳入同一bucket/identity白名单，保护全部嵌套业务/证明事实；已有任一新归属时拒绝破坏性降级。

本片超过8文件，按迁移/领域、预览消费者、评估及主动学习消费者拆到基于当前本地HEAD的独立工作树；主树负责显式接管、旧夹具、恢复、正式文档/合同与实际验收。最小选择是分别保存bucket，但无法识别端点变化或保留绑定/历史核验事实，因此复用现有严格envelope；不引入另一套通用对象表或复制冻结语义。最脆弱前提是旧清单足以独立解释当前副本；不满足时保持NULL与读取/清理拒绝，不能从对象文件名推断可信内容。

```text
新建 Preview / Evaluation / Round -> 对应固定 storage_ref -> 专属输出前缀
            |                              |                 |
       独立输入 owner                 显式旧副本核验     回读 / 缓存核验 / 复用
                                           |
                        原有耐久清理行 / 受限离线映射
```

验证包括三个创建入口、重试/重放NULL拒绝、独立输入与输出/来源桶、同键不同桶、报告/PNG完整回读、候选回滚与提交不确定性、PUT未过期/CPU租约/活动claim拒绝、清单缺失/额外/变更、撤权/Key变化/Gate排空/取消真实收尾、逐项清理失败及恢复白名单。只使用核实owner的一次性test资源；真实HTTP/MinIO、两个当前Worker和浏览器验证输出路由及保留副本，不声称真实模型框架训练资格。正常hooks、Ruff、生成API/SDK合同、文档及diff检查通过后提交。人工3100/8100、两个Worker/beat、既有业务与原件/冻结字节保持；迁移及源刷新只在所属环境执行。回退保持新增事实和未知归属拒绝，关闭新写入即可，不恢复全局桶猜测；完整用途切换继续关闭。

## 本轮实施定案：E02-B4 训练实验输出的固定归属

在干净基线 `bfcd8d083` 继续执行已有整套实施授权。当前TrainingRun的权重、检查点、评估/原生配置/观测摘要及脱敏日志仍按全局training_bucket读写；模型和初始化只引用这些原对象。下一片固定每个实验的projects/{project}/runs/{run}/整个前缀，以同一owner覆盖所有attempt、重试续训、模型安装/下载/初始化、评估及删除责任。数据版本仍使用已有独立归属；增强预览自己的输出、基础权重及其他用途另行收敛。本片不开放整个用途切换，不改Executor/共享训练合同、冻结config/provenance/初始化pin，不重建镜像。

0221为TrainingRun增加可空内部storage_ref，沿用现有locator envelope，purpose为training-run且prefix严格按项目/实验UUID限定。新建API、自动策略、参数搜索三入口在首次执行/I/O前显式固定；普通new_attempt/续训不能为NULL旧位置自动猜测。所有run输入输出URL、物理读取和派生脱敏日志使用保存端点/桶，模型通过RESTRICT来源run/attempt解析归属，不复制物理信息进冻结语义。先复核所属attempt键再I/O；NULL或身份变化明确拒绝，但取消/故障收尾仍保留原资源租约规则。

历史只接受负责人显式POST /projects/{project_id}/training-runs/{run_id}/storage-pin。全部attempt终态且无保留GPU租约后，沿用有界条件完整读取/真实收尾的Reader及维护permit；候选仅取已持久绑定。逐项核对DB已发布或上传清单的SHA/大小/attempt身份，已发布对象缺失、未知键及未解释上传拒绝，不反序列化权重。脱敏日志只在受支持当前确定性变换可从独立核验的源日志重建完全相同字节时接管；未知历史修订不猜测。发布前复核操作者/Key、实验及全部attempt指纹、候选/Gate修订，保存当前保留副本证据，GET不写接管。

删除同事务将run归属复制至独立tombstone，提交后清理；NULL旧run/dataset意图不做物理删除。受限离线恢复只映射bucket/identity，保护全部其他嵌套事实，新增run合成恢复证据。有run归属/意图时迁移拒绝破坏性降级。回退保留additive事实与读取拒绝，不重新采用全局桶。

本片超过8文件：领域/迁移、实验流/观测和模型/初始化/评估分配到当前HEAD的独立工作树；主树负责旧副本接管、清理/恢复、集成/旧夹具、正式文档与真实环境。复用现有owner/Reader/角色权限，不新增依赖、环境变量、凭据或服务。

```text
API / 自动策略 / 参数搜索 -> persisted run.storage_ref -> attempts / uploads / finish
                                      |                        |
旧实验显式核验 -> 同一归属             |                        v
                        模型 / 初始化 / 续训 / 报告 / 日志读取
                                      |
                  committed tombstone -> 同端点清理 / 离线受限映射
```

验证覆盖新建三入口、旧NULL拒绝、同键不同桶、续训和初始化分开归属、模型/报告/日志、旧副本完整/缺失/冲突/撤权/维护/真实收尾、提交失败零删除、失败意图持久、受限迁移/恢复。使用核实owner的一次性test环境，真实API/MinIO上传、摘要核验及URL下载证明路由，不以合成executor回执声称真实框架训练资格。正常hooks、Ruff、OpenAPI/SDK、文档与diff检查通过后提交；清理任务产物，保留人工3100/8100、两个Worker/beat及既有业务字节，再记录Outcome。

## 本轮实施定案：E02-B3 训练版本冻结前缀的固定存储归属

在干净基线 `3ce93cad0` 上复核并实施，沿用整套 Epic 持续实施和提交授权。现有版本仅保存 package_key，ZIP、独立冻结图片/掩码、派发与预览源、重试及删除均可能重新读取当前 training_bucket。新片固定版本自有前缀的归属，不开放整个 training 用途绑定切换，不修改 Executor/共享训练合同，不重建 API/ML 镜像。

0220为 TrainingDatasetVersion及TrainingStorageDeletion增加可空内部 storage_ref；结构保存已登记平台Location/配置修订、training绑定代次、精确endpoint/bucket/prefix/物理身份及new或verified-retained来源。新版本在首次物理写入前于创建事务固定；所有重试、取消恢复、包发布及未知提交应答清理始终使用该归属。prefix严格为projects/{project}/datasets/{version}/，版本字段不可被普通更新改写。解析旧归属只核对实际同端点client，不按当前training_bucket或新绑定重新解释；端点、未知身份或非法子键在首个I/O前拒绝。

迁移不根据环境变量回填历史。增加项目负责人显式POST /projects/{project_id}/training-datasets/{version_id}/storage-pin，GET保持零接管写入；候选只取已持久training绑定及平台位置，不顺带bootstrap。排除pending/running，先取消并等实际构建退出；使用同一版本advisory执行锁，在物理核验前后比较版本状态/attempt/snapshot/manifest/package与候选指纹，最终CAS保存归属与核验范围/清单摘要，不改snapshot、manifest或对象字节。已有pin幂等，普通重试不自动接管未知旧位置。失败/证据不足保留所有对象与记录。

显式接管有界盘点整个精确前缀，条件完整读取、SHA/大小、关闭及末尾盘点复核后才提交。ready必须验证发布ZIP完整摘要/大小、唯一安全成员、内外manifest一致、逐图/标签，以及必需保留媒体/掩码；合法媒体集以全部snapshot样本为准，去重不能把未入ZIP样本误判垃圾。多余未知键、无法解释的旧attempt包、缺失必需文件或冲突均拒绝。failed/cancelled允许尚未物化成员和空前缀，但已有副本须对应固定snapshot的独立SHA及实例内容；只有对象自身metadata.sha256而无独立内容证据时返回legacy_content_identity_unproven，不扩大为已证明历史原始位置，也不隐含新增源读取。接管只表示核验当前保留副本，不证明独占或未来不可变。

下载、analysis图片/掩码（包括sample evaluation）、训练assignment.dataset_url与增强preview源URL统一使用版本归属，不改历史preview.spec/input_digest或冻结run.config。删除在原事务复制storage_ref至独立tombstone，提交后才物理清理，身份不符或未知旧dataset清理意图不执行；训练运行产物的原清理合同独立保留。新版本尝试清理仍先复查已发布package引用，提交确认丢失不能删除已发布包。

离线恢复明确验证源版本及意图的归属，再在既有物理映射白名单中改bucket/identity并记录映射账本；不得改prefix、接管证据、snapshot、manifest、包SHA和业务ID。数据库只允许拥有表的恢复工具在显式事务标记下进行这一受限物理映射；运行角色不能改已固定归属。未知旧dataset tombstone保持待核对，不凭当前配置补造端点后删除。

```text
version creation -> persisted storage_ref -> export Worker -> retained media/masks + ZIP
legacy explicit pin -> bounded full verification -> same persisted storage_ref
                                                            |
           download / image-mask read / run source / preview source
                                                            |
          committed deletion intent(ref) -> same-origin cleanup
          offline restore(ref) -> validated physical mapping ledger
```

范围超过8文件。领域/迁移子代理在当前HEAD隔离树负责两模型、0220、training_dataset_storage解析/分配及DB回归；消费者子代理在独立树负责Worker、analysis、training run与preview源及针对性回归；主树负责API显式接管、严格旧副本核验、删除/恢复、集成测试及正式文档。无新依赖、环境变量、凭据owner、镜像或用途切换。现有Fernet/MinIO凭据不复制到ref；3100/8100、业务数据、两个Worker与beat保留。

验证：新创建/重试跨当前桶变化仍使用原位置，同键陷阱拒绝；图片/原生RLE/派发/预览/下载各入口；旧ready与部分/空接管、去重、元信息自洽但无独立SHA拒绝、未知attempt/键、变更/预算/关闭异常不接管；删除提交失败零I/O、失败意图保留、重试不碰其他桶及发布应答未知保护；迁移/不可变约束、恢复映射与源事实保持。仅用核实owner的一次性test命名空间，实际Celery+MinIO构建/取消恢复/旧副本接管/选桶与隔离恢复；正常hooks、Ruff、OpenAPI/SDK合同、文档与diff检查。按owner清理测试与等价集成树，刷新所属Worker并确认原人工内容不变后追加Outcome。

回退保留additive列和固定事实，未知身份不降级回全局桶；已有固定归属时迁移拒绝破坏性降级。完整E02还需其余用途owner、切换/迁移及浏览器写入资格，E10真实框架加载集合对账仍需另行部署资格，本片不称完成。

## 继续实施定案：E02-B1/B2

基线 `61d8ed982`，工作区干净，迁移单头 0209。E03/E04、严格原件读取、来源诊断及筛选冻结已落地；本轮不重复它们，也不因本轮交付开放用途绑定切换。人工环境 3100/8100 与其业务数据保留；Orca 中继不可用，修改串行完成。不构建镜像、不改 Compose、不引入依赖。

- B1：0210 为 StorageConnection 增加独立 revision，既有行初始化为 1。GET/列表返回 revision；PATCH 必填 expected_revision，缺少返回422，旧修订返回409。所有接受的编辑递增连接修订；仅实际配置变化或显式轮换凭据才递增关联位置 config_revision。旧 API 调用者必须先读取修订；不提供忽略 CAS 的兼容旁路。
- B2：更新配置或凭据时使用候选副本，复用白名单、Fernet 和现有 S3/SFTP 连接测试。外部 I/O 在 connection/location 行锁外执行，整体等待有上限；S3 只列至20项且关闭客户端，SFTP 认证/通道设超时并按迭代器最多取20项，不全目录列举。这里只证明 API 节点能认证并列举，不证明 read/range/write/预签名或扫描 worker 能力。
- 验证成功后按 connection → location 加锁，重读账号/管理权限、API Key 原证明、连接 CAS、最新白名单与位置引用；失败、超时、撤权或竞争均不覆盖原配置/密文。受引用连接仍拒绝实际配置改向，保持名称编辑与凭据轮换。错误只返回固定分类，不回吐底层异常/凭据/签名 URL；现有测试入口同步使用有界、安全分类并拒绝旧修订结果。
- UI 编辑固定打开时的 revision，保存失败保留草稿；冲突明确要求取消后重新打开，不自动把旧草稿套在新 revision 上。名称单独编辑不发送配置；配置/凭据保存显示验证说明。列表测试结果按 revision 隔离，防止旧成功显示在新配置上。

范围超过8文件，准确 owner：`apps/api/app/db/models/storage_connection.py`、`alembic/versions/0210_storage_connection_revision.py`、`schemas/storage_connection.py`、`services/storage_connection.py`、`api/v1/storage_connections.py`、`tests/test_storage_connections_api.py`、`tests/test_storage_locations_api.py`、新增 `tests/test_storage_connection_updates.py` 与 `tests/test_migration_0210_storage_connection_revision.py`；Web `api/storageConnections.ts`、`components/connections/StorageConnectionsPanel.tsx` 及其测试，受新增必填 revision 影响的测试 fixtures；OpenAPI 快照/本地类型、README、CHANGELOG、三份连接器用户/API/架构文档与两份执行计划。若读取发现其他契约消费者，按必要兼容更新记录。

```text
编辑表单（固定 revision） -> PATCH + expected_revision -> 候选配置/密钥
                                                        |
当前权限/白名单 -------> 有界 API 节点列举验证（行锁外） <-+
                                                        |
账号/Key/白名单复查 -> connection CAS -> location 引用保护 -> 原子发布
                                                        |
                       独立连接 revision + 必要的位置 config_revision
```

验收：隔离 test 数据库验证迁移往返/历史密文不变、缺失修订、两方旧表单竞争、失败/超时保留旧值、验证中并发更新/删除/撤权/白名单变化、名称不失效探测、引用改向拒绝及安全错误；复用旧连接/位置/原地 S3/SFTP 回归，Web 验证固定修订和保存草稿、测试结果隔离，SDK/OpenAPI、类型、lint/CSS、文档与 diff 检查。真实隔离 e2e 环境用受控本机 MinIO 端点验证正确/错误凭据轮换、旧版本冲突与原值保留，检查编辑弹窗在桌面/窄屏和明暗主题的截图、溢出、控制台/请求。只清理本轮测试资源。随后迁移人工环境并确认可用，不重建镜像。

回退：先回退客户端/API 后可保留 additive revision 列；需要降级迁移时只移除 revision，不触碰密文和位置数据。已生效的外部凭据不自动撤销旧凭据；外部凭据撤销及运行任务兼容窗口由部署者控制。完整 E02 仍保留用途历史定位、绑定切换、写入/浏览器能力资格及停用影响工作。

## 本轮实施定案：E02-A

基线 `fde06cd33`。动态注册采用“部署允许根 + 数据库内位置/子目录”，不把每个数据集路径写进环境变量。新增根内子目录无需重启；新增未挂载宿主机目录仍先由运维映射。当前没有经用户授权可用于验收的真实业务根，本轮用隔离临时目录验证，不改实际 Compose volumes。

最小替代是仅配置一个根并做字符串前缀判断；它不能满足动态注册、节点可达性和路径替换保护。本轮复用 E01 四表中的 StorageLocation/Binding、现有连接器授权与 Fernet，并使用 Linux openat2 的 beneath/no-symlinks 限制。默认允许根为空；不支持安全打开的节点明确不可用。

### 范围与契约

- 新配置 `STORAGE_FILESYSTEM_ROOTS` 是 root handle 到部署绝对路径的 JSON 映射，默认 `{}`。只在服务端配置保存绝对根；API 仅收 handle 和规范相对路径。受控根下拒绝符号链接、跨子挂载和非常规文件；只读代码不写探针或缓存。
- Alembic 0195 扩展 StorageLocation 的 connection_id/enabled，新增逐节点探测记录；老 S3/SFTP 连接保持原 ID、owner/global 权限和加密凭据 owner，只关联位置、不复制秘密。旧连接更新增加位置修订、失效旧探测；删除仅清理无引用关联，有内容/绑定引用则拒绝。
- 新 `/api/v1/storage-locations` 提供过滤列表、详情、超管注册 filesystem、带 expected_revision 的元数据/授权/启停更新、单层目录浏览、超管节点探测与探测查询；`/roots` 只列允许根句柄，`/bindings` 只读列出全部九用途，不在 GET 中 bootstrap。
- filesystem 位置仅 super_admin 管理；project_admin 仅按 global 或显式 user ID 授权浏览；员工的任务读取权限不授予目录浏览。连接关联位置继续委托旧 can_use_connection。输出不含绝对根、连接凭据或原始文件系统异常。
- 目录浏览默认 100、最多 200 项，一层最多检查 5000 个原始条目；超限明确失败。游标绑定位置、修订、路径、目录身份和条目摘要，目录变化要求重读。每页复核权限，临时 FD 始终关闭。
- 节点探测分别记录 api 与指定 Celery hostname。使用 Celery worker_direct 专属队列，执行端校验真实 hostname、权限和修订；离线/超时/晚到/旧修订不能成为有效证据。list/read/range 分开报告；只有显式 sample_file 的常规文件会被只读采样，空目录不会把 read 标成已验证。证据有效期 5 分钟。
- 根和位置子目录按每个节点的路径指纹、dev/inode 固定身份；不同节点不直接比较 dev/inode。首次 worker 探测信任部署者的同名根映射，后续变化拒绝静默重绑；它只证明节点能力，不证明多节点字节相同，该门由 E05 内容核验负责。
- 不增加 DatasetSource、原图 HTTP resolver、原地注册任务、用途切换或 UI 页面；后继 E04/E05/E03/E09 消费这批接口。现有复制导入保持可用，S3/SFTP 不能凭关联位置冒充原地读取已验收。

### 文件 owner 与验证

本切片超过 8 文件：模型/迁移（dataset_asset、models 注册、0195）、配置（config、.env.example）、filesystem 安全打开器、storage_location 服务/schema/router、storage_connection 服务和路由、Celery probe/配置、audit、API 快照/生成客户端、SDK 覆盖清单、专项测试、正式文档/ADR/CHANGELOG/DEV 及本系列导航。不新增依赖、密钥或外部服务。

```text
部署根映射 -> 安全打开器 <- API 注册/浏览 <- 当前位置授权
                 ^             |
                 |             v
指定 worker 专属队列 -> 节点探测 -> StorageLocation + 版本化节点证据
既有连接器 ----------> 关联位置（权限/凭据仍由原 owner 管理）
九用途固定绑定 ------> 只读展示；历史对象定位不切换
```

验证覆盖正常目录/中文空格、新增子目录、路径穿越/绝对路径/链接/替换/特殊文件、预算/游标/FD 释放；授权与撤销、配置 CAS、旧连接更新/删除、九用途缺失与共桶、GET 零写入；API 可达 worker 不可达、指定节点与过期/旧修订拒绝。使用隔离 test 数据库及临时根；运行旧连接器与 E01 回归、迁移往返、OpenAPI/SDK 契约、文档构建及 diff 检查。若启用本 checkout worker，核对任务注册并执行真实指定节点探测，结束清理所拥有的进程、资源和构建产物。

回退应用时保留新位置元数据并关闭新登记入口；旧复制导入与固定平台桶继续可用。仅无内容/绑定引用的关联位置能清理，不能为回退删除原目录。本轮不分配发布版本，不构建后端镜像。

## 本项目基线与改造落点

下列为固定提交的静态证据，尚非运行/浏览器验收结果。

| 当前 owner / 代码锚点                                                      | 已有能力、缺口与复用约束                                                                                        |
| -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| [storage_connection.py](../../apps/api/app/services/storage_connection.py) | 复用 can_use_connection/assert_connection_admin；当前 owner/global、create 写 project_id=None，未实现项目共享。 |
| [crypto.py](../../apps/api/app/core/crypto.py)                             | 复用 Fernet；访问凭据轮换与 CONNECTOR_ENCRYPTION_KEY 轮换分别处理，后者需要重加密和恢复材料。                   |
| [connector_guard.py](../../apps/api/app/services/connector_guard.py)       | 保留白名单与多入口复检；客户端实际连接仍会重新解析 DNS，不能将 DNS pinning 写成已有能力。                       |
| [sftp.py](../../apps/api/app/services/sources/sftp.py)                     | 现有测试主要验证列举；list/open 不能直接当作本地安全根打开器或持续原地读取证明。                                |

候选修改入口：[storage_connection.py](../../apps/api/app/db/models/storage_connection.py)、[storage_connection.py](../../apps/api/app/services/storage_connection.py)、[storage_connections.py](../../apps/api/app/api/v1/storage_connections.py)、[config.py](../../apps/api/app/config.py)、[docker-compose.yml](../../docker-compose.yml)。新增表、适配器、迁移、测试和准确文件范围在晋升时固定，不直接由逻辑实体生成数据库。

## 1. 问题与代码依据

当前外部 S3/SFTP 连接配置已提供个人和全局作用域、密钥加密等能力，适合复用；它主要服务拉取导入，尚不能表达所有原图长期存放位置。当前 `project_id` 字段不能直接解释为已经实现项目级连接共享。[连接模型](../../apps/api/app/db/models/storage_connection.py)

平台内部业务对象通过全局 `StorageService` 和多个桶分流。应让“数据在哪里”和“平台某类文件写到哪里”成为两个可明确维护的概念。[存储服务](../../apps/api/app/services/storage.py)

Docker 部署已支持选择 MinIO 宿主机数据目录；本 Epic 新增的是受控目录和多位置注册，不是修补“容器不能持久化”的问题。[部署配置](../../docker-compose.yml)

## 2. 目标与边界

目标是建立一份可信的存储位置目录：管理员配置物理连接与允许根，数据管理员只在有权限的位置中选取数据源。平台托管文件通过用途绑定写入指定位置，已有对象仍按其实际位置读取。

本期包括：

- 服务端只读目录、已有 S3/SFTP 连接配置归一化、默认托管 S3 位置。
- 能力声明与探测、配置版本、凭据轮换、停用影响分析。
- 平台九类现有存储用途的显式绑定和兼容初始化。
- 为 Agent 类型预留协议，不要求 M1 实现 Agent。

本期不包括：在页面任意浏览宿主机、远程执行挂载命令、自动开放所有目录、给数据管理员返回明文凭据。SFTP 配置可被统一管理，但持续预览和训练读取要通过 EPIC-05 能力测试后才开放，不能因“可导入”就宣称“可原地使用”。

## 3. 用户故事

| 编号    | 用户故事                                                                                | 交付切片              |
| ------- | --------------------------------------------------------------------------------------- | --------------------- |
| E02-S01 | 作为管理员，我能把已挂载目录登记成只读存储位置，限定根目录，供授权人员选取子目录。      | M1                    |
| E02-S02 | 作为数据管理员，我能看到可用位置及其读写、扫描、预览能力，不能选择不受支持的模式。      | M1                    |
| E02-S03 | 作为运维，我能配置内部访问端点与浏览器访问方式，验证上传和读取，而非只验证能否连接。    | M1                    |
| E02-S04 | 作为管理员，我能维护托管文件用途绑定，同时保留老对象的实际定位。                        | M1/M2                 |
| E02-S05 | 作为运维，我能轮换凭据和停用位置，并看到受影响的数据集、任务和版本。                    | M2，最小停用规则进 M1 |
| E02-S06 | 作为项目负责人，我能申请使用指定来源而不接触全局连接密钥；授权边界由 EPIC-11 统一定义。 | M2                    |

## 4. 建议模型

以下字段和接口均是设计草案，尚非现有实现。

| 实体              | 关键字段                                                    | 约束                                                                          |
| ----------------- | ----------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `StorageLocation` | `id/name/kind/config_revision/site_id/enabled_state/health` | 名称可改，ID 稳定；种类为 `filesystem/s3/sftp/agent` 等；启用状态与健康分开。 |
| 位置非密钥配置    | `root_handle/endpoint/bucket/base_prefix/access_mode`       | 对普通用户屏蔽绝对根、内部地址和不必要的拓扑信息。                            |
| 位置密钥引用      | `credential_ref/credential_revision`                        | 复用现有加密方案；页面只返回“已配置”和修改时间。                              |
| 能力快照          | `list/read/range/write/delete/presign/version_id/checksum`  | 区分声明、验证结果、验证时间和具体执行节点。                                  |
| `StorageBinding`  | `purpose/location_id/prefix/generation`                     | 决定新写入目标，不追溯改变旧 `AssetLocation` 或历史对象引用。                 |

九类初始用途：`annotations`、`datasets`、`bug-reports`、`media-cache`、`audit-archive`、`import`、`export`、`avatars`、`training`。首次迁移绑定到现有端点和现有桶，保持对象键不变。一个用途可以映射同一位置内独立桶或前缀；是否共享桶由生命周期和权限需要决定，不强行合并。

`StorageLocation` 是物理连接边界，`DatasetSource` 是某个数据集在该位置上的接入规则。一个位置可被多个数据集引用；一个数据集可在后续关联多个来源。

历史头像、附件、审计归档、训练包等没有 DatasetItem 的对象，也必须保存可解析的 `StoredObjectRef` 或固定 `binding_generation`。该逻辑引用可复用既有业务字段，不要求全部新增成数据集样本。某用途的历史对象尚不能固定定位时，禁止独立改其新写入绑定；可以在完成整用途迁移和对账后原子切换。

## 5. 功能与失败语义

### 5.1 受控目录

- 根目录由部署者预先配置或挂载，应用以只读权限访问；数据集创建者只选位置和相对路径。
- API/worker 哪些节点能访问该根必须显式探测。API 能看到而 worker 看不到时，位置不能标记为“可扫描”。
- 相对路径规范化后必须仍在允许根内；默认不跟随符号链接，尤其拒绝越界链接、`..` 跳转和设备文件。
- 路径校验与实际打开之间要防止路径替换；实现需采用受控根下的安全打开方式，不能只做一次字符串前缀判断。
- 使用固定 root handle 关联部署挂载。根目录改到另一处必须生成配置修订和重新验证，不得让现有引用悄悄指向同名新文件。

### 5.2 连接和能力探测

- `list` 成功不等于 `read` 成功；只读来源不执行写入探测。
- 托管位置的写入探测仅在专用测试前缀内创建并清理探针对象，记录残留失败。
- 浏览器预签名 URL 需验证签名、外部域名、代理路径、CORS 和 Range；内部 worker 读取单独验证。
- 连接超时、认证失败、根目录未挂载、无读取权限分别呈现；日志不包含密钥或完整签名 URL。
- 不支持能力用明确错误和前端禁用提示呈现，不能默默退回一次全量复制。

### 5.3 变更、轮换和停用

- 并发编辑采用配置版本条件更新；测试失败不覆盖最后有效配置。
- 轮换新凭据先验证，短暂兼容现有任务；失效后重试应重新取凭据，不在任务参数里长期存密钥。
- 停用位置先阻止新接入和新写入；既有读取如何继续需显式策略。强制停用必须给出影响清单。
- 有引用的位置不能物理删除配置；先归档。移除连接配置不删除源文件。

## 6. API 与界面契约草案

| 建议操作       | 输入/输出重点                          | 页面归属                         |
| -------------- | -------------------------------------- | -------------------------------- |
| 查询位置列表   | 权限过滤、能力、健康摘要；不得回吐凭据 | 存储页；数据集向导仅提供选择器   |
| 创建/修订位置  | 位置类型、受控根或连接配置、配置版本   | 存储页                           |
| 测试位置       | 按执行节点返回分能力探测结果和时间     | 存储页                           |
| 浏览来源子目录 | `location_id + relative_path + cursor` | 数据集接入向导；受服务端权限约束 |
| 变更用途绑定   | 旧/新绑定、影响摘要、有效版本          | 存储页管理功能                   |
| 查询引用影响   | 数据集/活动任务/冻结版本计数与授权深链 | 存储页详情                       |

界面不把“连接正常”描述为“所有数据可用”。数据集只显示来源位置名称、原图可用性、最近扫描时间和“查看存储位置”；详细健康、凭据、容量仅由存储页维护。

## 7. 验收标准

| 编号     | 可验证标准                                                                                                                 |
| -------- | -------------------------------------------------------------------------------------------------------------------------- |
| E02-AC01 | 登记只读根后，授权用户能浏览根内子目录并选取；提交根外路径、越界符号链接或无授权位置均被服务端拒绝。                       |
| E02-AC02 | API 可访问而扫描 worker 无法访问的目录，显示具体执行节点不可用，并阻止开始原地注册任务。                                   |
| E02-AC03 | 只读来源不能被选为托管写入目标；允许从它读取并复制到另一个有写入能力的目标。平台不向只读源目录写入探针、缩略图或临时文件。 |
| E02-AC04 | 九种现有用途完成初始化后，已有上传、标注附件、训练包、头像和媒体读取仍定位到原对象。                                       |
| E02-AC05 | 改变用途绑定只影响后续新对象；历史对象依据其位置记录读取，且数据集名称变更不改变路径。                                     |
| E02-AC06 | 根目录或端点修订必须通过验证和影响检查；同名新根不自动被视为已有 Asset 的等价副本。                                        |
| E02-AC07 | 凭据轮换后新请求成功，旧凭据不出现在响应、日志、任务详情；轮换失败仍可使用上一有效配置。                                   |
| E02-AC08 | 停用被引用位置时返回授权范围内的影响清单；既有引用保留且可标记不可用，外部原件不被删除。                                   |
| E02-AC09 | 同一配置被两人并发编辑时，后提交的旧版本请求被拒绝，不覆盖已经生效的新配置。                                               |
| E02-AC10 | 某用途新写入切到位置 B 后，位置 A 上的历史头像、附件和训练包仍由固定位置/绑定代次正确读取；历史定位未完成时切换被阻止。    |

## 8. 迁移与回滚

1. 以兼容配置加载现有全局存储端点，生成默认位置和用途绑定，不移动字节。
2. 将现有连接器逐条映射到新位置或关联记录，保留原 ID 映射、owner/global 语义和加密密钥。
3. 先双读配置/单一写入，新位置模块通过灰度开关对试点项目开放。
4. 新位置创建的数据由新读取链路处理；回退旧 UI 不得令此类数据失联，必要时关闭新接入并保留新读取兼容服务。
5. 回滚配置时恢复上一有效修订，不能把已产生的新对象重新解释到旧绑定；对象位置记录是读取依据。

## 9. 测试、风险与待决策

- 测试位置类型与能力组合、只读 mount、worker 挂载缺失、凭据轮换、目录边界与并发配置更新。
- 在真实部署拓扑验证内部/外部 URL；单测不能替代反向代理和浏览器链路验收。
- 确认 M1 的受控根由环境配置还是管理员 UI 登记：建议环境配置允许根，UI 只能从中登记，减少误选风险。
- 确认个人连接成为团队数据源时的授权迁移：建议由有管理权限者显式转换，不能因项目关联就自动分享连接。
- 确认 `StorageBinding` 首期是否只读展示并通过配置变更：建议读写切换跟随 EPIC-07 迁移控制流程。

## 10. 工程任务与完成定义

- [ ] 定义位置、用途绑定、配置版本和能力响应结构及数据库迁移。
- [ ] 为九类用途逐一盘点消费者、现有定位字段和历史对象引用，补齐非 DatasetItem 对象的固定定位与切换门槛。
- [ ] 兼容现有连接器和默认 S3 配置，编写幂等映射与回退操作说明。
- [ ] 实现受控目录解析、执行节点探测和有界目录浏览。
- [ ] 实现配置测试、轮换、归档与引用影响查询；接入 EPIC-11 审计。
- [ ] 对接数据集来源选择器、EPIC-09 位置详情、EPIC-05 读取路由。
- [ ] 通过上述验收，交付目录部署示例、权限说明、操作手册及兼容测试记录。

完成后，用户能选“数据在哪个位置”，应用能判断“这个位置能做什么”，原图无需先进入托管桶。

## 本项目推进切片与晋升条件

运维提供一个授权只读根及 API/扫描/读取节点挂载映射；后端固定部署允许根接口。根变更必须产生配置修订。S3/SFTP 持续引用分别通过能力验收后开放。

| 切片  | 独立结果           | 交付范围                                                                               | 验收 / 回退边界                                                            |
| ----- | ------------------ | -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| E02-A | 兼容连接与只读位置 | 现有连接关联位置、受控 root handle、配置修订和节点探测；九用途先兼容初始化并只读展示。 | 只读源不写探针；现有 owner/global 不扩大；API 可达而 worker 不可达时阻断。 |
| E02-B | 轮换和受控用途变更 | 能力测试、停用影响、配置版本更新；历史定位固定后才开放新写入绑定切换。                 | 新对象写 B、旧对象读 A；失败轮换保留上一有效修订。                         |

前置合同不等于等待前置 Epic 全量完成。每个切片合并后系统仍须可用；含新来源的回退保留兼容读取和授权，不能把其相对路径交给旧 S3 拼接逻辑。预计多数切片涉及超过 8 个文件，实施时明确大范围边界与准确清单。

已有回归入口：[test_storage_connections_api.py](../../apps/api/tests/test_storage_connections_api.py)、[test_connector_crypto_guard.py](../../apps/api/tests/test_connector_crypto_guard.py)、[test_storage_public_url.py](../../apps/api/tests/test_storage_public_url.py)、[StorageConnectionsPanel.test.tsx](../../apps/web/src/components/connections/StorageConnectionsPanel.test.tsx)。这些仅为旧行为回归起点，新合同/状态/故障场景须补有意义的用例；不能以旧测试通过替代本 Epic 验收。分层命令、真实浏览器矩阵和产物清理见[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md#本项目验证与文档交付)。

实现时同步：[connector-security.md](../../docs-site/dev/reference/connector-security.md)、[storage-connections.md](../../docs-site/user-guide/datasets/storage-connections.md)、[docker-compose.md](../../docs-site/ops/deploy/docker-compose.md)，以及适用 API 指南/快照/前端类型、`CHANGELOG.md` Unreleased。新增配置同步 `.env.example`、`pnpm docs:gen-env-vars`、`DEV.md`；涉及 SDK 合同按对应 skill 核验。正式 ADR 在实施时分配可用编号。

完成实施后按[计划规范](../README.md#required-completion-steps)追加 `## Outcome`，列实际提交、正式文档、验证证据、清理与剩余限制；草案清单不等于已实现。

## Outcome

- Landed changes: E02-A dynamic filesystem registry, legacy connector association, ACL/CAS, read-only directory pagination, versioned per-node capability probes and read-only nine-purpose bindings. Included in the commit containing this outcome; no release milestone assigned.
- User documentation: `docs-site/user-guide/datasets/storage-connections.md`; current UI includes dynamic locations, source diagnostics and verified reference registration/reading delivered by successor slices.
- API and developer documentation: `docs-site/api/guides/storage-locations.md`, `docs-site/dev/concepts/storage-connections.md`, `DEV.md`, generated environment/API references and SDK coverage manifest.
- Deployment documentation: `docs-site/ops/deploy/docker-compose.md`; unified roots and explicit container/image distinction. No real business mount or backend image was changed.
- ADR: `docs/adr/archive/0092-controlled-storage-registry.md`. CHANGELOG: Unreleased entry added.
- Verification: 29 focused tests passed, including two real direct workers with readable/missing-root configurations; 106 compatibility checks passed and the shared-bucket case passed after correcting its fixture owner. SDK OpenAPI contract: 4 passed. Fresh/reversible/forward/restore migration validation passed through 0195 with no leftover owned databases. Final real offline-worker timeout and referenced-location rollback guard passed. Documentation build, ADR/frontmatter and internal-link checks passed. Base/production Compose parsed with empty/synthetic root maps.
- Remaining work: E02-B binding-switch safety and non-Item historical object identity, location retirement/impact and full write/browser capability qualification. E04 scanning, E05 strict reads, E03 registration and E09 primary UI are now delivered in their own plans; they are not remaining E02 prerequisites. This Epic stays active.

### E02-B1/B2 继续交付

- 本提交完成独立连接 revision/CAS（0210），候选配置与凭据在行锁外验证、保存前重新核对账号/管理权/原 Key/修订/白名单/引用，失败不覆盖旧值。名称编辑及相同配置不使位置证据失效；显式凭据轮换会失效旧探测。受引用位置仍禁止实际配置改向或删除。
- S3 只列至20项并关闭客户端；SFTP 按迭代器采样并在截止时关闭传输，严格位置复用部署固定公钥。请求等待预算与后台 SDK 收尾分别说明，不把列举通过当作完整读写或多节点资格。
- UI 固定打开时的修订；候选失败和409保留草稿及内联原因。SFTP 历史缺省 auth_type 的改名不发送配置；测试记录按修订隔离。嵌套编辑支持独立 Escape、焦点返回与窄屏滚动。
- 正式文档：README、CHANGELOG Unreleased、`docs-site/user-guide/datasets/storage-connections.md`、`docs-site/api/guides/storage-connections.md`、`docs-site/dev/concepts/storage-connections.md`；API 快照与本地生成类型同步。未分配发布版本。
- 本地验证：API 127项（含独立提交的并发/删除/撤权/白名单/引用竞争、密文保留、SDK错误脱敏、SFTP截止、0210往返及S3/SFTP/导入回归）；Web 11项、SDK合同4项，共142个不同用例。类型检查、完整Web lint/CSS检查通过；保留4条既有ESLint警告及2条既有样式建议。文档196份frontmatter与198页导航通过。最终提交运行完整hooks和diff检查；不代表远程CI。
- 实际运行：隔离3200/8200经真实MinIO验证错误凭据返回400且保留原revision=1/可用凭据，正确现有凭据显式保存使连接revision=2/位置revision=2，随后外部并发改名推进至4，旧表单返回409并保留草稿。最终GET确认名称为并发赢家、位置修订仍2、已有凭据测试成功；没有更改共享MinIO凭据或创建外部IAM账号，独立新凭据的原子替换由专项用例证明。
- 浏览器检查1440×1000/390×900明暗主题截图，无横向溢出；窄屏可滚至操作区。实际重新打开、取消、Escape及焦点返回已核验；等候表单真正卸载后再操作，避免将弹窗过渡帧当成页面故障。当前会话请求除预期409外没有其他失败，控制台无错误；首次会话的预期400已由保留草稿及原凭据仍可用证明。
- 人工3100/8100保留原业务数据并应用0210，原管理服务刷新当前checkout API/两个worker，运行时端口覆盖固定为原地址；beat保留。不重建镜像。隔离test/e2e资源和本轮日志/截图/密钥/缓存在交付前按归属清理。
- 整个E02仍未完成，不归档本计划；下一步按总队列收敛引用消费者和历史对象定位，再开放受控切换。

### E02-B3 训练版本固定存储归属

- 0220迁移 `2c8d6c3d7` 与领域实现 `e301ea3ff` 为训练版本及独立删除责任保存内部 storage_ref。新建和自动策略在首次存储I/O前固定；消费者 `91161af0d` 统一构建/重试、独立图片/掩码、包下载、训练/增强派发和模型试用的输入定位。后续全局training桶变化不重新解释已保存归属，实际端点变化和越界键在I/O前拒绝；权重、运行产物仍使用其原独立合同。
- 本交付增加项目负责人显式 storage-pin、严格旧副本验证和删除/恢复集成。GET不接管，迁移不猜测历史位置；候选只取已登记绑定。核验整个版本前缀及ready包、全部snapshot媒体/掩码（含未入包去重成员），缺少独立内容身份、遗漏分区/成员、伪造RLE消费证据、未知attempt或额外键均拒绝，不读取当前原件补证。版本执行锁、维护permit、进程容量和发布前权限/Key/上下文复核覆盖真实收尾，关闭未知仍保留责任。
- 删除先提交原归属的tombstone，再清理原桶/前缀；提交失败零I/O，存储或确认失败保留重试责任。未知旧版本意图不执行物理删除。离线恢复只有表owner配合显式事务标记可改bucket/identity；prefix、endpoint、来源/核验证据及原snapshot/manifest/ZIP不变。有pin时拒绝破坏性降级。
- 正式文档提交 `73b9e75cf` 更新README、CHANGELOG Unreleased、训练用户/API/架构、存储架构与恢复操作说明；本交付同步OpenAPI、生成路由索引及SDK维护风险分类。不分配发布版本，不改依赖/配置/Compose或镜像。
- 本地验证按classname/name去重：322个API/DB/纯用例、13个恢复工具用例、4个SDK合同用例，共339项通过；检测合同不适用的RLE receipt用例明确跳过1项，不计成功。覆盖数据库不可变/受限恢复、创建/自动策略、严格旧副本、Key撤销/轮换/过期/缩权、同键不同桶陷阱、删除提交及重试。Ruff检查/格式、OpenAPI/生成类型、Web类型检查、正式文档构建与正常提交hooks通过；这些为本地结果，不代表远程CI。
- 实际隔离18100/13100与两个新启动Worker/MinIO：检测、原生RLE各构建3图并下载核对包SHA；改变当前用途桶后仍读取原独立图片/掩码。复制出的NULL历史ready版本分别完整核验4/7对象并接管，重复请求保持原证据，删除只清理其专属前缀；另两条空前缀cancelled历史版本显式接管后重试至ready。真实浏览器打开两类版本/样本预览，图片加载及RLE画布正常，无控制台错误、失败资源请求或横向溢出；本轮未改UI样式，不将这些结果声称为完整视觉矩阵。
- 0220隔离恢复演练销毁合成源后恢复151表93行、九桶20对象，核对2个版本和1条tombstone的固定归属映射、冻结媒体、嵌套保护事实及原件历史，cleanup=verified。生产RPO/RTO与共享外部站点资格仍未验收。
- 人工3100/8100已应用0220并刷新所属两个Worker/beat，最终doctor健康。原1条旧ready版本经显式部署核验4对象后保存归属，不改snapshot/manifest/包字节；8Dataset、12Item、8Source、12Task、7Annotation、1TrainingVersion、24对象和12源文件与交付前完全一致。一次性test数据库/Redis/九桶已销毁并独立确认不存在；测试凭据、日志、截图/缓存和已等价集成的隔离树在交付前清理。
- 完整E02仍保持执行中：其他非Item用途的历史定位、受控新写入切换及完整浏览器写入能力尚未交付；不归档整项。E10真实框架加载集合回执、C2全部固定内容读取/独占与物理回收仍单独过门，不因本片已固定训练输入位置而视作完成。

### E02-B4 训练实验输出固定存储归属

- 0221迁移 `5a97d42b3` 和领域实现 `d921b82f7` 为TrainingRun保存内部固定归属，严格覆盖项目/实验整个前缀与全部attempt。API、自动策略、参数搜索三个新建入口显式分配；普通重试和续训不为旧NULL记录猜测位置。数据库限制正常改写，恢复仅允许owner配合事务标记映射bucket/identity，有运行归属或意图时拒绝破坏性降级。
- 消费者 `b58ad11e3`、`2d0087134` 统一上传、完成核验、原生配置、观测摘要、检查点/续训、脱敏日志、评估、模型注册/安装/下载和初始化。数据集输入仍独立解析；模型通过同项目来源Run/Attempt及已发布权重解析，不把物理位置复制进冻结config、初始化pin或provenance，不修改Executor/共享训练协议。未知位置的失败/取消准确回执仍可保留原GPU租约边界并收尾，成功回执不得绕过存储核验。
- 集成 `275a6c7dc` 增加显式Run storage-pin：只核验全部attempt终态、无保留GPU租约的旧实验，候选只取持久绑定；以独立DB发布/上传清单核对全前缀实际SHA、大小和attempt metadata。已发布对象缺失、成功执行遗漏、未知额外键及变化均拒绝；失败/取消的未发布对象允许缺失，存在则必须核验。派生日志须从独立核验源重建当前脱敏结果并匹配字节/修订。维持有界读取、共享进程容量、durable permit与真实关闭；发布前重新核对当前管理权/Key、所有attempt、候选及Gate修订。GET不接管，重复显式请求保持原事实。
- 删除同事务复制固定归属到独立tombstone，提交后按原桶清理；NULL旧实验返回409且不创建意图，不执行物理I/O。旧NULL run/dataset意图保持责任并拒绝物理清理。恢复映射同时覆盖Run与两类意图，仅排除许可的物理子字段，保留全部嵌套语义/证据。既有夹具 `80bbd0a56` 及本集成改为规范Run/Attempt键和独立输入/来源/输出桶；并发研究用例改用唯一资源并清理空闲租约。
- 正式文档 `20595c95e` 更新README、CHANGELOG Unreleased、训练API、实验/模型用户指南、训练架构和存储恢复说明。OpenAPI、生成类型/路由索引与SDK维护风险分类同步；未修改版本、依赖、共享配置、Compose或镜像。
- 本地按classname/name去重：455个API/DB/纯用例、15个恢复工具用例、4个SDK合同用例，共474项通过，最终无未解决失败。包含不可变/受限迁移、三入口、独立来源桶、NULL/越界拒绝、撤权/轮换/过期/缩权、对象与Gate变化、取消等待真实线程、未知关闭保留责任，以及删除提交/重试。Ruff/格式、生成合同、Web类型检查、完整文档构建、正常提交hooks和diff检查通过；这些是本地结果，不代表远程CI。
- 实际隔离18100/13100、两个新Worker及MinIO：输入与输出分桶，真实HTTP签名PUT六类产物、完成摘要核验、全部下载、脱敏日志、评估及模型注册/权重下载通过。私有核验进程将当前桶改为另一已拥有桶后，六个原产物的完整流核验、评估和模型URL仍指向保存桶；真实Celery处理Run tombstone只清理保存前缀，其他桶同名诱饵保留。复制的历史NULL实验在接管前拒绝下载/删除，显式核验5对象后幂等读取/清理通过。本片使用合成执行器回执与权重字节，未执行模型框架训练，不构成GPU或框架消费资格。
- 实际浏览器打开完成实验的产物页并点击下载，下载权重SHA与独立清单一致；截图、无横向溢出、页面错误与失败资源检查通过。未改UI样式，不声称完整明暗/窄屏视觉矩阵。离线演练在销毁合成源后恢复151表96行、九桶22对象，包含2个版本、1个Run及2条删除意图；运行产物SHA、固定归属/嵌套保护事实及既有原件历史保持，cleanup=verified，生产RPO/RTO仍未资格化。
- 人工3100/8100已应用0221并刷新所属两个Worker和beat，最终doctor健康；8Dataset、12Item、8Source、12Task、7Annotation、1TrainingVersion、0Run/Attempt/Model、24对象及12源文件与基线一致，原snapshot/manifest/包与storage_ref保持。一次性test数据库、Redis及九桶已销毁并独立确认不存在；浏览器已关闭，五个干净且patch等价的隔离树/分支已移除，任务凭据、日志、截图和测试缓存清理。
- 完整E02继续执行，不归档整项。下一片先复核训练预览、样本评估及主动学习等非Run输出的历史定位；完整用途切换继续关闭。E10实际框架消费回执、真实跨站点/迁移目标、生产独占与物理回收仍按各自合同推进。

### E02-B5 预览、样本评估与主动学习输出固定归属

- 迁移及领域 `c4aef5671`、迁移回归 `e8ef51653`、预览消费者 `7f52d6a39`、评估／选样消费者 `5babd2e62`、恢复辅助 `25e976fac` 和集成 `d7ecbddf3` 已提交。0222为三类现有 owner 增加严格可空归属及不可变／受限恢复约束；只在新建事务分配，所有输出读取、签名、报告缓存前核验、候选PNG复用和候选报告清理使用保存桶与准确键。冻结spec、媒体描述、推理／执行器wire及摘要不变。
- 三个显式 storage-pin 核验完整保留前缀；独立发布／上传回执或冻结派生SHA解释每个对象，预览名称限于自身count。预览所有CPU租约和PUT期限必须结束；评估／选样当前尝试须终态，旧lost只兼容非当前、更旧代际、claim已清空且保存完成时间的封存记录。终态评估残留claim不是活动任务。核验不证明旧线程停机，不回源修复；旧评估报告不借重试后的Sample行冒充原证据。取消等待真实关闭，关闭未知保留容量和耐久访问记录，最终重核权限／Key、上下文、绑定及Gate代次。
- NULL历史仍可读取元数据和安全停止；成功预览详情可返回report=null，显式报告／PNG继续409。Preview原行及EvaluationRun删除意图行保留清理责任，CPU／业务锁不跨物理删除；未知位置和失败逐项保留，AL不新增删除入口。恢复只映射三类归属的bucket／identity，所有嵌套事实保持；有pin时拒绝破坏性降级。完整用途切换仍关闭。
- README、CHANGELOG Unreleased、训练API与生成路由索引、配置／评估／主动学习用户指南、训练及存储架构、恢复部署说明已更新；OpenAPI、前端生成类型及SDK维护风险分类同步。未修改版本、依赖、共享环境、Compose或镜像。
- 本地按最终有效classname/name去重：388个API／DB／纯用例、27个恢复工具用例、4个SDK合同用例，共419项通过；13个不适用的参数组合明确跳过，不计成功。覆盖严格迁移／恢复、创建与重试、独立输出、NULL零I/O和停止结算、完整历史核验、Key缩权／越权、上下文／Gate变化、取消等待及未知关闭、延迟清理和候选提交不确定性。Ruff、格式、OpenAPI／类型生成、Web类型检查、正式文档构建、正常提交hooks和diff检查通过；均为本地结果，不代表远程CI。
- 实际隔离18100／13100、两个新Worker及MinIO：三类NULL历史共4个对象经真实HTTP显式接管及幂等重放，预览PNG与评估报告完整回读通过。私有核验进程将当前桶改为另一已拥有桶并放入同键错误内容后，三类读取仍只访问保存桶，评估缓存命中仍复验。真实Celery完成预览过期及评估延迟清理，两个owner行和原前缀删除，其他桶同名诱饵保留。浏览器原生显示预览PNG，16×12尺寸和完整SHA与独立回执一致，页面错误为零；无UI样式改动，不声称全明暗／窄屏矩阵。使用合成保留产物，未执行模型框架训练或推理，不构成GPU／框架消费资格。
- 离线演练先销毁源再恢复151表108行、九桶26对象；新增3个owner、4个辅助对象及来源模型外键均验证，其他嵌套业务、内容及历史证据保持，cleanup=verified。生产RPO／RTO未资格化。人工3100／8100应用0222并刷新所属两个Worker及beat，doctor健康；24个对象、12个原件文件及全部业务表与基线一致，仅账号活动字段和追加审计日志正常变化。
- 一次性1822b025369a33f4:test数据库、Redis及九桶已销毁并独立确认不存在；浏览器关闭，四个干净且patch等价的隔离树／分支移除，任务日志、凭据、截图、构建与测试缓存清理。子代理曾误选含DB fixture的旧文件，默认annotation_test在0180因缺表失败并按env.py回滚；主树后续只读检查其仍为0179且无训练版本表，没有修改或删除该既有默认库。本片正式DB验证均在核实的一次性资源执行。
- 完整E02继续执行，不归档整项。下一步盘点其余平台用途及共享基础权重的持久写入者、读取者和清理者，先补齐不能安全切换的归属边界，再决定最小用途切换片。E10真实框架消费回执、真实跨站点／迁移目标、生产独占及物理回收继续按各自合同推进，不以本片合成结果替代。

## E02-B6 Outcome — 上传头像固定归属与耐久清理

基于 `5aad08281` 完成并提交计划 `ad29883bc`、领域 `eeba1ce65`、恢复与正式文档 `509cdc2bb`、消费者 `1b206ab00`，本记录随最终测试夹具修正提交。迁移单头0223。独立AvatarUpload保存严格avatars端点/桶/精确token键及完整SHA/大小，不改现有UserOut、随机token URL或客户端可选引用语法。pending先提交，真实PUT和完整回读结束后复核规范化前账号/请求原Key证明、引用CAS及Gate共享锁，原子发布ready和旧retired；失败或提交应答未知不猜测删除候选。匿名读取只读ready并完整核验实际字节后才返回200/304，历史没有独立归属的token返回404，需重新上传或选择内置头像，旧文件保留待核对。

retired记录兼任耐久清理意图，引用变化先提交，清理释放业务锁后沿保存桶精确删除并最终复查；失败保留重试，pending绝不按时间回收。cleanup队列新增周期任务，维护permit持续到线程真实收尾，未知Body关闭保留访问责任。恢复沿用既有表owner与事务标记，只改嵌套bucket/identity，不改token、账号快照、SHA/大小、状态、退休时间或用户引用。有任意记录拒绝破坏性降级。

验证共198项独立检查通过：66项严格归属/分配、54项实际PostgreSQL迁移/不可变/状态/受限恢复、53项上传与匿名读取/并发引用和账号撤权/Key两窗口变化/维护Gate/取消真实收尾/提交不确定/清理耐久回归、21项恢复工具检查、4项SDK合同检查。消费者首轮52/53通过，唯一失败是新故障注入夹具清理账号触及不可变audit外键更新；该独立测试隔离无关审计写入后，两种实际commit结果均通过，未修改审计保护。重复的两个用例不重复计数。Ruff/format、OpenAPI显式一致性、codegen、Web类型检查、文档构建、正常hooks和diff检查通过；接口合同未变化，无需版本号或SDK覆盖例外。

真实一次性test环境以13100/18100及两类当前Worker运行。在另一受控测试桶中构造三个历史owner夹具，同时在当前头像桶放同键错误字节：真实匿名HTTP读取保存桶完整SHA，条件请求304，pending/retired404；保存桶同大小损坏字节即使命中ETag仍409。重新上传正常图片后，新对象位于当前头像桶，旧ready行及原桶对象删除，当前桶同键陷阱仍在。实际Celery cleanup任务删掉一个无引用retired对象，保留pending字节和当前桶陷阱。浏览器独立会话实际显示两张256×256 WebP，完整取回字节并记录SHA，未见JS错误；本片无UI样式变更，不声称重做主题/窄屏矩阵。

实际离线恢复先销毁随机演练源，再向独立空目标恢复152张表、111行、九桶29对象；包括ready/pending/retired三行头像与三份实际WebP，完整字节、状态和账号引用核对通过，演练源/目标清理已独立验证。合成夹具只证明本次离线恢复，不授予生产RPO/RTO或真实框架/GPU资格。

人工环境已应用0223并刷新同一所属API、两个Worker与Beat，仍为Web3100/API8100，doctor健康。两个Worker均注册70个任务并包含新头像cleanup，队列符合原约定。刷新前后24个既有对象与12个原文件SHA完全相同；全部原业务表保持，仅迁移身份、正常账号心跳与审计变化，新增头像表为空，人工用户原先没有上传头像。未重建镜像，未改共享环境、依赖、Compose、凭据或发布版本。

关闭本轮命名浏览器会话，停止并销毁1822b025369a33f4:test，独立检查确认数据库、Redis与九桶全部不存在；两套隔离树经commit补丁等价和干净检查后移除。清理本轮缓存、构建与私有探针/报告，保留人工服务、业务、原件/冻结字节和规范工作树身份。整套E02仍为active，不归档；八用途盘点仍发现导出产物、报告附件、审计归档、普通媒体、旧标注文件和原件分支等当前桶消费者，完整用途切换继续关闭。基础权重实际由管理员工具按显式桶准备至节点缓存，训练assignment不临时从全局桶下载基础权重，不把该工具误记为未转换训练派发。

## E02-B7 Outcome — 异步导出产物归属、固定期限与耐久清理

计划 `a1494ad15`、请求授权及正式文档 `092a25d5b`、领域/0224 `93212332d`、消费者 `2351551dc`、最终签名/恢复集成 `e104fece8` 已提交。独立ExportOutput保存项目/Job快照、每候选UUID键、严格位置及SHA/大小和固定期限；不依赖源Job、Project或缓存行的存活。pending先提交，再沿保存桶上传并完整条件回读；失败、取消、发布应答不确定不删除字节，也不按年龄自动回收pending。最终ready、缓存和结果同事务提交，重复投递进度写入先锁定活动Job，避免把已完成的100%改回90%。ZIP内容合同及缓存指纹保持。

三个HTTP入口保留隐藏的原请求输出授权，与原引用媒体证明各负其责；不增加scope要求，Key轮换、撤销、权限/期限变化拒绝新发布。最终持共享锁，并在全部Output/缓存/Gate锁等待结束后、同步签名前再查自然期限。旧未完成Job没有该证明须重新请求，旧完成结果与通知URL不从当前桶重签。缓存只复用ready且位置/内容/索引相符的产物，完整SHA读取后再锁同一快照，只签固定期限剩余窗口；NULL旧缓存零I/O视为miss，已知缺失或损坏仅失效索引并重新构建。过期唯一键可安全替换，竞争有效索引保持。

过期清理先锁定并排除该Output的未收尾读permit，删除索引、提交retired后才精确物理删除，失败保留重试；退休后拒绝新读取准入。pending不进入自动清理，未知关闭保留耐久访问责任。迁移有事实时拒绝破坏性降级；受限恢复仍仅映射bucket/identity，项目/Job快照、内容、期限、状态和缓存关系保持。README、CHANGELOG Unreleased、导出/异步API、用户导出指南、数据流架构/SVG及恢复部署说明已同步，公开API合同未改变。

本地按最终classname/name去重：467项API/DB/纯用例、35项恢复工具、4项SDK合同，共506项通过；1个既有不适用批次状态组合跳过，不计成功。覆盖103项存储严格性和65项实际0224约束/受限恢复，三个入口JWT/Key、幂等原证明、最终时钟到期、所有共用导出路径、旧缓存/损坏/跨桶、竞争/独立键、取消真实收尾、commit应答未知、项目/Job删除和读/退休竞争。首轮两个旧final-guard夹具缺授权证明，已通过规范捕获补齐；另一个旧格式迁移测试依赖已替换的内部record签名，改用其原合同的NULL-owner历史索引并通过。重复用例不重复计数。Ruff/format、OpenAPI显式check、codegen、Web类型检查、文档构建、正常hooks及diff检查通过；既有mock异步通知告警及LiDAR重复目录告警保留，不代表远程CI。

真实18100/13100及两类当前Worker/MinIO验收：项目、批次和选中范围共五个导出完成，ZIP完整下载与独立SHA/大小相符；先将一个测试Output按受限恢复规则映到另一拥有桶，在当前桶放同键错误内容，随后缓存命中仍读保存桶、保持原expires_at，签名只剩604799秒。保存桶同大小损坏后生成独立新Output，旧owner/字节责任和当前桶陷阱保持。暂停所属export消费队列后由真实HTTP排队、撤销原Key、恢复消费，Job明确失败且没有Output。五条成功及一条失败通知与六个Job对应，成功URL/期限精确等于原结果，完成进度均100%。合成pending/ready/retired没有对应源Project/Job；真实打开GET句柄及耐久permit期间cleanup保留ready并删除另一retired，实际关闭后下一次Celery清理删除ready，pending及其他桶同键陷阱继续保留。

命名浏览器实际登录、从后台任务打开居中详情并点击下载新产物，ZIP完整校验及数据库SHA/大小一致，显示已完成/100%；无页面异常、失败资源或横向溢出。导出流程SVG同步嵌入源并在实际浏览器截图检查。应用没有样式变更，不声称重新完成明暗/窄屏矩阵。实际离线演练先销毁随机合成源，再恢复153表115行、九桶32对象；三态导出ZIP和ready缓存关联、既有头像/冻结/其他证据保持，cleanup=verified。仅资格化本地合成恢复，不代表生产RPO/RTO、真实训练框架或GPU资格。

人工环境已应用单头0224，刷新所属API、两个Worker及Beat，仍为Web3100/API8100且doctor健康。两个Worker各注册70个任务及原队列，新增导出表为空；24个既有对象与12个原件文件逐字节SHA保持，全部原业务表保持，仅迁移身份、正常账号活动与审计追加变化。未改依赖、共享环境、Compose、凭据、版本或镜像。

一次性1822b025369a33f4:test已停止并销毁，独立重新检查确认数据库、Redis及九桶均不存在；命名浏览器关闭，两棵干净且commit补丁等价的隔离树/分支已移除。清理本轮私有探针/下载/截图/日志、构建与测试缓存，保留人工服务及规范工作树身份。完整E02仍为active，不归档；下一步盘点报告附件、审计归档、普通媒体、旧标注文件及其他历史消费者，继续逐项固定归属。完整用途切换、生产独占/物理回收、E10真实框架消费及未指定的跨站点/迁移目标仍须独立实施。

## E02-B8 Outcome — 报告附件独立归属与认证字节传输

计划 `f1629c086`、领域/0225 `36fbb6d5d`、恢复集成 `38f4ee1c1`、后端流程 `819a5a62e`、路由索引 `5e53fde71`、WebP错误边界 `a5f9f0f68`、测试隔离 `8fea66eb1`、Web交互 `db0d6c270` 和局部错误处理 `0a1b1e53c` 已提交。独立BugAttachmentUpload保存上传者/报告UUID快照、精确位置、SHA/实际大小/MIME/安全文件名及固定180天期限；账号或报告删除不丢失物理责任。raw认证上传先提交pending，再条件创建和完整条件回读；真实收尾并复核原账号/Key、同一回执及Gate后发布ready。失败、取消和提交应答未知不补偿删除，pending不按年龄回收。损坏WebP帧的EOFError也统一返回422。

报告创建同事务首次绑定本人ready、未过期且未消费的最多五个附件；服务端回执与首附件别名必须一致。下载经Bearer认证，使用UUID读取许可完整读取保存桶、核对内容，末端再查当前可见性/登记关系、原账号/Key、期限和Gate后返回私有不可缓存的图片字节。旧upload-init合法请求返回409及新路径，不再签发最终键PUT；旧无归属报告元数据保留，下载409，不猜当前桶。Web两个下载入口均取认证Blob并释放临时URL，按报告/账号/会话隔离进行中和错误/重试；提交在逐次上传及创建前后保留原会话围栏，成功回执可在同次重试复用。局部错误不会再额外触发全局403/5xx通知。

删除报告先同事务退休已知owner，提交后才精确删除。cleanup队列每日有界重试已过期ready与retired，读取许可或未知关闭继续阻止物理删除；失败保留独立责任。应用不再为bug桶设置整桶180天lifecycle，实际部署须确认旧规则已移除。0225有事实时拒绝破坏性降级；受限恢复仅映射bucket/identity，上传者、绑定、内容、状态、固定期限不变。正式知识已同步至README、CHANGELOG Unreleased、`docs-site/api/guides/bug-reports.md`、反馈架构、BUG管理用户指南及恢复部署说明；OpenAPI、路由索引和本地生成类型已更新。

按最终classname/name去重，322项API/DB/纯用例、21项恢复工具、45项Web和4项SDK合同，共392项通过。覆盖真实三格式/大小和像素预算、严格0225/受限恢复、两窗账号/Key变化与自然到期、一次绑定/别名、同键异桶与损坏、取消实际线程收尾、未知close/commit、删除事务和读许可竞争。首轮测试夹具中的独立permit删除曾被业务SAVEPOINT回滚复活，已改为只清理本测试准确UUID/位置的已关闭假句柄许可；手工Request补齐method，超长文件名按驱动实际SQLSTATE22001断言。最终相关项全通过，重复项不重复计数。Ruff、正常提交hooks、OpenAPI显式check、codegen、全Web lint/CSS tokens与类型检查、文档生成检查和build、diff检查均通过。既有SQLAlchemy事务告警、React Router提示和文档高亮/分块提示不代表远程CI。

真实18100/13100、MinIO及两个当前Worker验收：PNG/JPEG/WebP原字节上传与回执一致，报告首次绑定成功；无Bearer、他人读取/消费和重复消费被拒绝。把测试owner按受限owner恢复规则映到另一拥有桶、在当前bug桶放同键错误字节后，读取仍使用保存桶；同大小损坏409、缺失404，均无签名跳转。实际chunked HTTP上传读体期间撤销原Key，发布被拒绝。真实打开MinIO GET句柄并持有UUID许可期间删除报告，owner退休且文件保留，Celery清理延期；实际关闭后下一次清理删除保存桶对象，当前桶陷阱保持。另一个过期ready被清理，181天pending和旧未知报告/文件保持；实际bug桶lifecycle为空。

命名浏览器实际登录，管理页下载与原图SHA一致；窄屏粘贴图片、认证上传200、报告创建201后，DB首次绑定及完整字节一致，在“我的反馈”下载同样匹配。保存桶注入损坏时仅当前附件显示可重试错误，恢复字节后重试成功、错误消失；三次下载的object URL均已revoke，下载SHA均为 `82b44c523f9ae49bc039d8b879a8bf0c12afcd63a2922451f66451470b284d8e`。管理和个人反馈的light/dark、1440×1000与390×844截图均已检查，无页面横向溢出或JavaScript异常；网络只有主动注入损坏的预期409，其余相关请求成功。

实际离线演练先销毁随机源，再恢复154表120行、九桶36对象；附件三态/实际PNG、ready报告绑定、已删除账号/报告的退休责任和旧未知附件均保持，cleanup=verified。仅资格化本地合成恢复，不代表生产RPO/RTO或真实框架/GPU资格。人工环境已迁移单头0225并刷新API、两类Worker及Beat，仍为3100/8100且doctor健康；两个Worker各注册71个任务及原队列，人工bug桶旧lifecycle已移除。原业务表保持，仅迁移、正常账号活动与审计追加和新增空表变化；原24对象/12文件逐字节SHA不变。无依赖、共享环境、Compose、凭据、版本或镜像变更。

一次性1822b025369a33f4:test已停止并销毁，独立检查确认数据库、Redis和九桶全部不存在；离线演练资源也已验证清理。命名浏览器关闭，三棵干净且提交补丁等价的隔离树及分支已移除；测试探针/下载/截图/日志与缓存/构建产物按本轮归属清理，保留人工环境和规范工作树身份。E02仍为active，不归档；完整用途切换继续关闭。

下一独立消费者阶段先定稿审计归档。当前归档仍从当前桶读写固定月键，PUT后直接DROP源分区，没有独立候选回执、完整回读、源内容/分区OID与边界锁定；并发INSERT可逃出旧快照。审计任务的失败、取消和终态提交也需一并核对；现有管理页没有归档UI，不把仅API列取当成已完成前端。实施须先固定独立尝试、完整内容证明和真实源分区锁，再把DROP与ready/审计置于同一事务；旧未知归档不得按当前桶接管。部署权限并不统一：基础/生产维护连接走owner，LAN脚本另有runtime持有审计树DDL权限，必须按实际角色验证，不能靠单Worker/Beat或迁移advisory锁冒充归档互斥。可在一次性本地库核验真实分区DDL、并发写入与源销毁后的恢复；生产独占、Object Lock、RPO/RTO和大分区性能仍需独立目标。

## E02-B9 Outcome — 审计归档固定归属与源分区事务保护

已完成本地交付；领域/迁移 `66d874913`、认证读取 `772a49911`、源分区协议 `224ebd995`，单gzip成员和显式游标收尾分别由 `9fb10d0c7`、`aa60b7ecb` 补齐；Worker/取消、恢复和正式文档集成 `c5844f69b`。三个隔离树的提交已逐项验证补丁等价并集成。0226单头增加永久独立归属；不改版本、依赖、共享环境、Compose、凭据或镜像。

每次候选固定源父/子OID、完整UTC自然月、规范JSONL与压缩回执和精确保存位置。先在明确READ COMMITTED事务锁child并生成有界归档、提交pending且释锁，再条件创建及完整读回；最终新事务重新计算全源内容，采用父/子NOWAIT升级锁，将DROP、ready/发布时间、审计及作业月度结果一起提交。并发源写入、改名重建、锁冲突、维护变化、取消或提交应答不确定均不授权猜测删除。永久pending不自动重放或按年龄清理，同月ready阻止删除后来重建的源分区。

核验发现并修复两项真实边界：标准gzip读取会接受额外成员/零填充，现以有界zlib逐块拒绝所有额外物理字节并校验完整尾部；SQLAlchemy/asyncpg的result.close没有实际关闭服务端portal，现以唯一具名DECLARE/FETCH/CLOSE管理自身游标，成功/异常/重复取消都收尾，不关闭无关游标。FETCH每次一行，避免大行批量缓冲；本片没有资格化大月分区吞吐。归档失败持久写failed和固定分类，保留此前成功月；重复投递只读既有状态，不重新开始旧候选。取消与发布共用Job锁，历史有账号的Job保留终态通知，通知与取消采用一致User→Job锁顺序。

279项不同本地检查通过：252项API/DB/存储/作业用例、23项恢复边界及4项SDK合同。按JUnit classname/name去重并取最新结果，修复后的52项分区检查和10项Worker检查整体通过；不把失败前的运行重复累计。覆盖严格0226/restore/禁止删除、完整语义/单成员/预算、两窗原账号与Key/角色/自然期限、保存桶与末尾坏行、关闭未知许可保留、真实并发INSERT与父路由锁升级、同名换OID、不同隔离级别、取消/未知提交/多Worker及已提交部分结果。Ruff、OpenAPI导出/检查、codegen、Web类型、文档生成/build、正常提交hooks及diff检查通过。

真实维护验收使用全新PostgreSQL容器分离runtime与owner，并使用随机拥有的Redis/九桶；普通账号DDL和物化视图刷新被拒绝，五类maintenance任务全部完成。非空2001-01的两行使用相同id、不同created_at，完整归档、源DROP、ready/Job结果和三个返回项（含meta）均核对，两个Worker退出后连接为零，资源已销毁。修正容器就绪探针为TCP，避免误把初始化阶段的临时Unix套接字服务当成最终可用数据库。

另在本工作树一次性test通过实际HTTP、MinIO及两类当前Worker归档2003-01两行，完成Job和独立SHA/大小一致，API分页total=3且保留末条meta。将API当前audit桶配置指向另一个拥有桶并放同键错误内容后，读取仍来自已保存桶；旧固定月份无归属409、同大小损坏409、已知对象缺失404、无Bearer401，恢复合成字节后再次200。浏览器登录保留的人工环境，打开现有后台作业居中详情并实际检查截图、Escape、控制台和横向溢出；认证归档列表200且为空，未知月份409，未新增归档页面或伪称完成新UI。命名浏览器已关闭。

离线演练先销毁随机源，再恢复156表124行、九桶39对象；ready归档源表保持不存在，pending源表保留且目标OID不同，历史父/子OID及源/压缩回执、Job快照、状态和发布时间保持。两个文件完整解压核验，旧固定月份对象仍无归属。离线合成夹具仅在验证归属的随机库使用owner进行分区DDL、直接子表盘点和恢复映射，不给API或普通Worker传owner连接，不修改共享角色。cleanup=verified；此结果仅资格化本地合成恢复，不代表生产RPO/RTO、Object Lock或独占权限。

人工环境已迁移0226并刷新所属API、两个Worker及Beat，仍为Web3100/API8100，最终doctor健康；两个Worker各注册71个任务，原队列保持。原24个对象和12个源文件逐字节SHA完全一致，其他业务表保持，仅迁移身份、正常账号活动/审计追加及新增空归属表变化。一次性test数据库、Redis和九桶已销毁并独立确认不存在；离线/maintenance资源也已验证清理。三棵干净且补丁等价的隔离树和分支已移除，私有探针/日志/截图与本轮测试、构建缓存按归属清理，人工环境及规范工作树身份保留。

完整E02仍为active，不归档；完整用途切换继续关闭。只读盘点确认评论附件、项目指南图片、独立Task原文件、持久RLE和掩码导入输入仍有当前桶定位，已有Dataset原件归属也尚未贯通全部媒体消费者。下一片优先评论附件：保留通用文件及20MiB单文件能力，收敛上传回执、同上传者/标注首次绑定、活动原评论授权读取和90天期限/软删宽限后的耐久清理；Feedback镜像只保留元数据，不产生新owner或续期。当前清理读取storage_key而实际保存storageKey，且删除失败仍清空元数据，须与归属协议一起消除。其余消费者另片，不重新建立已有Asset/缓存/Training/Export所有权。E10真实框架消费、生产独占/物理回收和未指定的跨站点/迁移目标继续独立推进。

## E02-B10 Design — 评论附件固定归属与认证文件传输

状态：已完成本地实施、验收与提交，结果见下方Outcome。用户已授权持续实施、提交及自行选择并行，不等待Orca或真实跨站点目标；本片不代表完整E02完成。

### 边界和可见行为

- 保留标注评论通用文件、空文件及每文件20MiB上限；不采用BUG附件图片白名单或10MiB限制，不新增附件数量上限。任务留言、Issue与Feedback原生附件不扩展能力。
- 新认证raw POST `/annotations/{id}/comment-attachments/upload?file_name=...` 有界读取请求体，返回现有四字段Attachment回执。旧upload-init合法请求409并给新路径，模型错误仍422。下载保留原路径及兼容query，改为认证完整文件200，不再提供最终键PUT、签名URL或302。
- 评论创建仅首次绑定本人、同一实际annotation→task→project、ready、未过期且未消费的回执；文件名/类型/大小/键必须匹配服务端事实，重复键拒绝。正文、提及、画布和现有权限保持。历史读取仍允许软删/取消标注及归档Dataset；上传和绑定要求活动标注。
- Feedback复制字典只保留元数据，不产生新owner、读取授权或续期；不扫描镜像JSON作为保活依据。未知历史JSON及字节保留，下载409，不猜当前桶或自动接管。

### 耐久事实

0227接0226，新增独立CommentAttachmentUpload：UUID、上传者/annotation/task/project快照（均无级联FK）、严格annotations用途storage_ref、SHA256、实际大小0..20MiB、安全文件名、规范MIME、created_at、固定2160小时expires_at、pending/ready/retired、首次bound_comment_id、首次retired_at及last_attempted_at。键固定 `comment-attachments/{annotation_uuid}/{upload_uuid}`；文件名为独立元数据。MIME保留通用type/subtype，规范小写和去参数，缺省octet-stream；文件名拒绝路径、控制字符和点目录。

先提交pending，再If-None-Match条件创建和完整条件回读。真实收尾、末端授权和Gate核验后发布ready；失败、取消或提交结果未知不得补偿删除，pending不按年龄回收。事实/期限不可变，绑定仅ready首次，状态向前，退休时间首次固定。恢复只有表owner配合既有事务标记可映射bucket/identity，其余嵌套事实不变；任何owner存在时禁止破坏性降级。

### 事务和准入

```text
原账号/凭证 + 实际讨论可见性
  -> 提交pending或读取意图，释放业务锁
  -> Location durable permit，acquired后重新复核并提交
  -> 保存桶准确键条件IO、完整SHA/大小、真实关闭
  -> 新事务按同序重新锁定并复核 -> ready或完整字节
```

评论router已持User SHARE，禁止升级UPDATE。此片锁序为User SHARE→原ApiKey SHARE→Project SHARE→ProjectMember SHARE→TaskBatch SHARE→Task SHARE→Annotation SHARE→Comment UPDATE→按UUID排序owner UPDATE→Gate SHARE。资源锁使用NOWAIT、populate_existing和no_autoflush；预读只定位，锁齐后重核关系，变化拒绝，不追锁新关系。冲突409。复用现有讨论可见性，不引入Dataset写门禁或标注编辑锁。

流程模块保存原Key hash/scopes/expires证明并校验原Bearer；JWT保存exp/gen/jti并沿现有Redis尽力策略复核，不新增scope。流读、permit等待及对象IO不持业务锁。准入前、acquired回调和末端均复核当前账号/角色、原凭证、实际上下文、owner期限/绑定及Gate；最后等待后再核自然到期。下载只有原活动Comment登记的ready owner可读，permit purpose使用准确UUID；完整核验后再授权，缺失404、变化/未知409、过期410。强制attachment、nosniff、private no-store，关闭未知继续保留耐久读取责任。

### 删除、期限和部署

- 90天对象年龄与软删7天宽限独立，最早截止生效。评论软删、项目删除、Dataset unlink和孤儿Task硬删均在真实事务内退休已知owner；原Comment/账号消失不丢责任。标注自身软删不退休附件。
- 复用purge_soft_deleted_attachments任务、队列及Beat名称。有界扫描ready到期、绑定原评论消失/非活动及retired；读取permit或未知关闭阻止物理删除，失败保留完整owner/key/ref及JSON，成功只删除已知责任。pending及未知历史不自动清理。GC不在owner后锁Comment/Task；retired不能重新绑定，未到期且原评论活动继续保护。
- 只移除应用拥有的annotations桶 `comment-attachments-90d` lifecycle规则，保留无关规则，并核验实际部署，避免lifecycle绕过permit。运维自设的覆盖规则需核对，不擅自清除。
- 不改依赖、共享环境、Compose、凭据或版本，不重建镜像；保留人工3100/8100、两个Worker/Beat及24对象/12文件。应用0227并刷新既有服务，核验任务和队列。

### 分工、验收和回退

超过8个文件，三个隔离worktree基于本计划提交HEAD：领域/0227/严格存储及测试；认证流程/路由/清理及测试；Web API/交互及测试。主树负责删除调用点、lifecycle、恢复/演练、正式文档/生成合同、集成及实际验收。修改者分别提交，等价集成后清理；子树不安装或运行DB，DB测试仅主树走已验证一次性test模式。

覆盖严格约束/受限恢复、空文件/20MiB、通用MIME、同上传者/标注一次绑定、原账号/Key/上下文两窗变化和自然到期、同键异桶/损坏/缺失、持真实读句柄删除/到期、7天与90天边界、删除失败/未知历史不清空、Feedback不续期。实际HTTP/MinIO/Celery核验上传→绑定→读取/清理与保存桶陷阱；离线恢复先销毁合成源，覆盖三态、绑定、期限、原评论/账号已删除及旧未知，核对只映射bucket/identity和完整字节。

Web保留按目标回执、会话租约、逐文件即时接收，改认证Blob下载并增加scope/卸载取消，释放URL、隔离迟到错误。实际浏览器检查评论控件、明暗及桌面/窄宽、字节下载、失败重试、溢出和非预期请求。正式知识同步README、CHANGELOG Unreleased、评论用户/API指南、存储架构及部署/恢复说明，更新OpenAPI/生成类型和SDK风险分类。正常hooks及diff检查，结果按用例去重，不冒充远程CI。

有owner后不破坏性降级；回退保留0227和对象，旧客户端升级认证传输，不重新开放最终键直传。一次性数据库/Redis/桶、浏览器、探针/日志/缓存及已集成干净子树按归属清理，保留人工环境。Outcome记录实际结果，完整用途切换继续关闭。

## E02-B10 Outcome — 评论附件固定归属与认证文件传输

已完成本地交付。计划 `29970962b`、Web `239345423` / `6264866a2`、领域及0227 `b9c8638ad`、认证流程/绑定/清理 `151c1d3f9`、最终等待后的固定期限复核 `bd8b97c49`、删除/lifecycle/恢复与正式文档集成 `bdf11a827` 已提交；MIME数据库错误预期由 `91fbf5da9` 修正。四棵隔离树的全部独有补丁逐项等价集成，无未合并修改。未修改版本、依赖、共享环境、Compose或镜像。

独立CommentAttachmentUpload固定上传者、annotation/task/project/原Comment快照、严格九字段位置、实际SHA/大小/通用MIME/安全文件名及90天期限。先提交pending再条件创建和完整回读，最终复核后ready；首次绑定仅接受本人、同一实际上下文和准确服务端回执。失败、取消或提交应答未知保留责任，不按年龄删除pending。认证下载完整核验保存桶字节，保留原URI和兼容query，但不再返回匿名URL/302；历史未知409、已知缺失404、到期410。正文评论、软删标注及归档Dataset的历史读取边界保持。Feedback只保存元数据，不产生owner或延长期限。

账号/原Key或JWT、实际讨论关系、固定owner期限与维护Gate在准入、acquired及末端重新核对；User沿用SHARE，资源锁NOWAIT且不跨网络/permit等待。提交、executor排队、通知/刷新等最后等待后再次核对原固定期限，避免等待跨越自然到期仍发布结果。评论软删、项目硬删、Dataset unlink及孤儿Task清理在同一业务事务退休已知owner；物理清理沿保存桶与准确键，受真实读permit、90天年龄和7天宽限约束，失败保留owner和JSON，未知历史不猜删。原purge任务、队列及Beat名称复用，任务总数保持71。仅移除annotations桶中应用拥有的旧lifecycle规则，保留其他规则。

交叉审查补出真实评论的Feedback镜像外键遗漏：新增真实mirror夹具后，unlink/orphan两例明确失败；补齐先删镜像后删Annotation/Task，最终全部通过。孤儿入口用事务临时表冻结并锁定本轮任务，退休、计数与全部删除共用该集合；账号→Project→Task的NOWAIT锁防止重关联交错和反向等待，大集合不展开为大量UUID参数。新增回归确认冻结后出现的孤儿仍保留Comment和ready owner，不在本轮误删。项目删除回滚同时回滚附件退休。

472项不同本地检查通过：350项API/DB/存储/迁移/权限/期限/清理与删除、72项Web、4项SDK合同及46项恢复工具检查。按JUnit classname/name去重并取最新结果，最后82项流程回归整体通过；首轮MIME长度错误预期及镜像删除失败均已修正，不重复计成功。Ruff/格式、OpenAPI导出及一致性检查、codegen、Web类型、CSS tokens与lint、正式文档生成/完整构建、正常提交hooks及diff检查通过。这些是本地结果，不代表远程CI。

一次性18100/13100、MinIO与两个当前Worker完成17项实际检查及5次Celery清理：空文件、20MiB、任意二进制、认证/越权/首次绑定、保存桶陷阱、缺失/损坏/恢复、未知旧JSON/字节保留，分别验证。真实MinIO读句柄与durable permit的保留/关闭/延迟清理为独立内部验收，不冒充被阻塞的HTTP请求。7天和90天使用不同的不可变历史夹具，未改旧owner时间；活动Feedback不能保活到期附件。期限补丁后刷新一次性两个Worker，最终HTTP上传/绑定/完整下载再次通过。

浏览器实际选择通用文件、发送评论并下载，原269字节文件、界面新上传25字节文件及空文件均核对，创建的三个Blob URL均有对应revoke。历史附件409提示正常；最终版本将一个已知对象替换为同大小坏内容，浏览器拒绝下载，恢复原字节后点击同一按钮成功，SHA为 `444ce405d5d3db5c01df8ce0b2c15dec8d0af463360a22e73cbbc4534a69373d`。截图检查1440×1000和1024×1000明暗主题，窄窗使用现有讨论浮窗，未见横向溢出或JS异常。首次默认讨论dock高度不足时按钮被内容裁切，隐藏标注检查器后按可见控件完成操作；未为此推测性修改下载逻辑。合成ML端点不代表真实推理，最终smoke关闭该项目模拟绑定。

离线演练先销毁随机源，再恢复157表130行、九桶43对象。新增pending/ready/retired三态、空文件、原Comment/Feedback绑定、已消失账号/annotation/task/project/Comment及未知旧附件均完整对账；只映射bucket/identity，期限、快照、SHA、状态与首次退休时间保持。演练发现零字节PUT会触发SDK流封装/Expect兼容问题并阻塞同连接后续PUT；最小实测和新回归证明省略空Body后恢复正常，21项bundle测试及完整恢复通过，cleanup=verified。合成恢复不授予生产RPO/RTO或独占、Object Lock资格。

人工环境应用0227并刷新同一所属API、两个Worker和Beat，保留Web3100/API8100，最终doctor健康，两个Worker各注册71项且队列保持；实际purge任务派发成功，旧应用lifecycle规则已不存在。24对象和12源文件完整SHA与前基线相同；除迁移、新增空表、用户活动行与追加审计外，原业务表摘要保持，Job状态仍为45 completed/21 failed/1 cancelled。一次性test库、Redis及九桶销毁后独立确认不存在，浏览器关闭，四棵干净且补丁等价的隔离树/分支移除；任务私有探针、凭据、日志、截图及测试/构建缓存按归属清理，人工环境及规范身份保留。

完整E02仍为active，不归档，完整用途切换继续关闭。下一片先补核测试seed的namespace清理对新owner/permit的合同，再处理项目指南图片等其余当前桶消费者；本片实际测试清理使用整个已验证一次性环境销毁，未宣称旧namespace seed清理已支持新owner。独立Task原件、持久RLE、掩码导入输入及绕过既有Asset归属的媒体读取仍另片；不重复建设已交付的Asset、缓存与训练归属。E10真实框架消费、生产独占/物理回收和未指定的跨站点/迁移目标继续独立推进。

## E02-B10-A Design — 测试命名空间清理保留附件责任

基于 `7e791a1f3`，继续原实施/提交授权。当前 `_cleanup_e2e_fixtures` 先删业务行，再按当前annotations桶的整个annotation前缀删文件；新增CommentAttachmentUpload不随FK删除，保存桶、pending与真实读取permit均被绕过。只读复核确认这不是生产GC缺陷，而是隔离测试夹具清理合同缺口。本片不新增迁移、公共API、生产强制删除开关、UI或后台任务。

仅在既有非production、E2E开关开启且实际DB后缀为\_test/\_e2e的保护内执行。候选只按精确fixture Project UUID选择owner快照，包含Comment/Annotation已不存在的记录；不以上传者邮箱、未知JSON或当前桶推断归属。原业务锚点在所有物理阶段成功前保持。历史上已丢失Project/User锚点而不能归回namespace的旧owner不猜测接管，使用整个已验证一次性mode销毁。

1. 完整预检scope内owner的严格位置、不可变回执和状态；任何pending拒绝且零物理删除，不根据年龄假设上传结束。ready转retired并提交，原owner和fixture继续保留，首次退休时间不变。显式测试夹具销毁可清理终态附件，不修改生产90天/7天策略或事实。
2. 逐个冻结回执通过storage_access.run注册测试删除permit，短锁复核同一owner/state/ref及原读/写/删permit后提交，再沿保存桶精确条件删除并确认实际收尾。未知关闭、正在读取、维护阻断或删除失败均保留所有owner/业务锚点并明确失败；成功也暂不删owner。不得跨网络持User/Project/Comment/owner锁。
3. 既有媒体和旧格式评论前缀清理也移至最终DB锁之前。取消或重复取消必须等待实际线程收尾后才回滚和返回，避免迟到删除碰到相同命名空间重建的确定性媒体键。前缀清理永远不删除严格 `comment-attachments/{annotation_uuid}/{upload_uuid}` 键；它们只能由已登记owner授权。保存桶外同键陷阱及无owner规范键保留并明确报告残留，不据当前桶猜删。旧格式仅沿本fixture已解析annotation前缀，保留邻居和普通数据。
4. 所有物理阶段完成后，按账号→Project的顺序取NOWAIT锁，重新核对精确scope、owner集合与permit；新增候选或未知活动拒绝，不追删变化集合。只在同一最终事务删除已完成owner、原fixture及已有FK子行，残留断言包括owner；重新设置事务局部审计豁免，提交后才允许调用方重建fixture，避免带锁进入后续网络写。失败和并发冲突保留可重试锚点。

主树负责计划、文档、验证与真实一次性HTTP/MinIO验收；一个隔离修改者负责专用测试helper、seed集成及行为回归，独立提交后等价集成。不修改生产附件消费者。验证覆盖ready/retired、原Comment/Annotation消失、pending零I/O、打开读句柄/permit、其它写删permit、独立保存桶与当前桶陷阱、未知规范键、删除失败重试、scope变化、幂等重建及邻居保留；运行现有seed/附件相关回归。DB仅主树通过已核实一次性test环境执行，不使用默认annotation_test；保留人工3100/8100及其原件、两个Worker/Beat，不重建镜像。全部任务测试资源/产物按归属清理，正常hooks/diff检查后记录Outcome。

项目指南图片的下一片只读盘点并行进行；本设计不代替指南图片完整交付，完整E02及用途切换仍为执行中/关闭。

## E02-B10-A Outcome — 测试清理与训练种子归属收敛

本片已完成，集成 `fe7c6be3a`、`4d87b5dcd`、`c14ebffc6`；完整E02仍执行中，完整用途切换仍关闭。未新增迁移、公共接口、生产强制删除开关、UI或任务。

清理仅按本fixture精确Project UUID选独立附件责任，完整预检pending/位置/未收尾许可；ready先退休提交，沿保存桶精确删除且真实收尾，最终短锁重核固定集合后才同事务删除owner与原fixture。未知规范键和同键异桶陷阱明确阻断，不交给旧前缀猜删。物理失败、维护或并发变化保留可重试的数据库锚点。旧格式媒体线程取消后也须排空，重复取消不能让迟到删除碰到重建的确定性键；重建前旧事务已提交。fixture账号仍被外部评论引用时清理失败并回滚，不反向删除外部评论。

扩展回归另发现训练验收种子的实际遗漏：新建基线只flush和入队，没有调用正式训练接口的存储分配。现已为新建检测/分割版本调用同一allocate；重跑、failed/cancelled/running重试保留原ref和保存桶，历史无ref版本不自动认领。Fake provider补充真实端点元数据；测试共享Worker session时显式刷新回滚后的Project，不修改生产授权或归属规则。

主树在已验证一次性test环境完成118个不同本地用例：新附件清理22、seed router43、owned22、视频夹具14、训练种子17，零跳过；按最新结果去重，不累计重跑。实际旧代码重现两条回归：`d5336ee3c`清理200后owner仍在；`fe7c6be3a`取消时请求已结束而媒体删除线程仍阻塞。新实现均通过，覆盖打开Body/重复取消/未知关闭、四类permit、pending有无字节及年龄、缺少Comment/Annotation、保存桶陷阱、规范键残留、删除失败、维护、集合新增和跨项目已绑定评论。训练种子首次扩展回归有5处unpinned失败，修正实际分配遗漏后另修一处共享测试session的过期访问，最终17项通过；不将失败或中间重跑计为通过。

独立18100/13100的实际HTTP/MinIO完成9组检查。持有真实StreamingBody和同UUID内部读permit时cleanup409且原对象/owner/Project/Comment保持；实际关闭并结清permit后，绑定附件和空文件准确删除，邻居SHA保持。沿保存桶删除后，当前桶同键陷阱仍在且409；未知规范键同样保留，明确移除探针自建陷阱后可重试清完。200天pending无字节及有字节均409，九桶对象集合和SHA零变化，另一namespace仍可独立清理。该读permit探针不冒充阻塞中的HTTP下载。没有改UI，不重复浏览器明暗矩阵或声称远程CI、真实训练框架资格。

CHANGELOG、开发测试指南和E2E清理合同已同步；Ruff/format、正常hooks和diff检查通过。人工3100/8100健康、两类Worker及原服务身份保持，本片不对人工环境执行seed/清理，不重建镜像。一次性API/Web停止，DB、专用Redis及九桶销毁并独立查验不存在；任务探针、测试缓存与已集成子树按归属清理。下一片按下方B11设计实施指南图片；Task直传、持久RLE、mask输入、其余绕过Asset的媒体和E10真实消费资格继续另片。

## E02-B11 Design — 指南图片归属、共享引用与认证内容读取

状态：已完成本地实施、验收与提交，结果见下方Outcome。继续原整套实施/提交授权，不增加批准轮次或发布版本。实际入口为项目设置 `?section=annotation-guide`、Workbench GuidePanel和首页StartChecklistCard。实施前旧init/complete没有独立上传责任，HEAD只检查大小，最终PUT URL可重放；删除先物删且吞错。复制向导默认复制正文和原键，但旧读取又限定当前项目前缀，复制图实际404。显式ProjectUpdate还允许任意JSON覆盖清单；正文autosave只写annotation_guide，不参与此问题。

### 交付边界与权威关系

保留PNG/JPEG/WebP/GIF/SVG五种MIME、每文件1..5MiB、原Markdown `guide-asset:<key>` 语法、引用式图片/中文/空格/字面百分号、逐图上传、源码往返、表格图片、失败重试和放大图。图片内容保持上传字节，不引入转码或额外图片编辑能力。模板和AAP JSON继续只携带正文，不从正文自动生成图片引用。旧无独立归属清单保留精确元数据和字节，认证内容读取409并提示重新上传，不从当前桶猜读或自动补owner。

0228接0227，增加独立GuideAssetUpload及ProjectGuideAsset引用表，并为Project增加guide_assets_revision（初值1）。上传owner保存UUID、上传者/原项目UUID快照（无FK）、严格annotations九字段storage_ref、完整SHA256、实际1..5MiB大小、五种规范MIME、安全原名、created_at、pending/ready/retired及首次retired_at/last_attempted_at。精确新键 `projects/{original_project_uuid}/guide/{upload_uuid}`；文件名仅是元数据。事实不可变，状态只前进，pending不按年龄回收，任何owner存在时拒绝破坏性降级。受限restore仍只许表owner配合已有事务标记映射bucket/identity。

ProjectGuideAsset以(project_id,upload_id)为唯一关系，Project FK CASCADE、owner FK RESTRICT；created_at和身份不可改。新引用只允许在同一owner锁下对ready状态插入，退休后不能重挂或复活；发布上传时先ready再添加首个引用，同事务完成。引用表是读取授权和保活的真值，Project.guide_assets只提供原五字段外显清单/顺序及未知legacy条目。既有JSON或Markdown、原键前缀、拥有原项目权限都不能替代当前项目引用。当前项目有合法引用且可见时，即使原项目或上传者已删除，仍可沿独立owner读取。

### 写入、复制及删除合同

新raw POST `/projects/{id}/guide-assets/upload?file_name=...` 返回现有GuideAssetEntry；GET `/projects/{id}/guide-assets/content?key=...` 认证返回完整核验图片字节，private no-store/nosniff。旧upload-init/upload-complete/sign-url有效请求409，给出新路径；保留现有角色和不可见404语义，不新增scope。上传有界接收，先提交pending再条件创建/完整回读，最后在短事务重核原凭证、当前管理权、同owner和Gate，ready/项目引用/清单/修订原子发布。失败、取消或未知提交保留pending，不补偿删除。

显式PATCH guide_assets必须同时给期望guide_assets_revision；缺失/陈旧409，null、重复key和单独改修订422。新清单只能是当前已授权引用条目或现有legacy精确条目的子集/重排，不允许新增/改写元数据、跨项目注入或凭JSON挂载已知owner。所有上传、删除、显式清单PATCH在共同Project锁下从当前事实读改写，实际清单变化修订加1；正文autosave及其他窄字段PATCH保持现行为。数据库约束/触发器保护修订随清单变化，避免其它普通写入绕过计数。

copy_annotation_guide保持共享原键和正文：完成可能的backend /setup网络检查后，才锁源项目并重新核对当前可见性、正文、清单、引用及ready owner，将目标项目及新引用同事务提交。源项目读取不是最终授权；变更/撤权拒绝，不能凭早期payload复制。目标引用独立，来源删除图片或项目不能删除目标仍引用的内容。模板不复制引用，未知legacy仅保留原精确条目而不升级授权。向导文案更新为引用共享和独立删除语义。

DELETE guide-assets仅移除当前项目引用和对应清单；原JSON的未知legacy也可明确移除而不物删未知字节。最后一个权威引用移除时，在owner锁内退休；仍有其它引用保持ready。Project删除沿同一规则处理。新增周期清理先在owner锁内将ready且无Ref者退休并结束本次处理，覆盖测试seed/scale等直接Project CASCADE删除；下一轮才清retired，pending不纳入。清理不扫描JSON/Markdown当保活，不反向锁Project，已有或未知收尾读permit阻止物删。删除失败保留独立记录，重复删除精确幂等；没有7天或90天隐式图片过期策略。

### 访问与生命周期

原请求账号/Key hash-scopes-expiry或JWT exp/gen/jti在准入、acquired及末端重核，沿现有尽力Redis策略，最后提交/线程排队/通知等待后再核自然到期。复用现有凭证与短锁helpers，不重构已交付消费者。涉及项目时采用User SHARE→原ApiKey SHARE→相关Project按UUID排序→当前ProjectMember SHARE→owner按UUID排序→引用/Gate的顺序，默认NOWAIT、populate_existing和no_autoflush；写清单取Project UPDATE，读取/复制源取SHARE。实际浏览器证明并行图片上传、正文autosave和预览会在同一Project行产生短时竞争；普通单项目上传、读取和删除对Project行采用事务内1秒lock_timeout有界等待，等待后继续重核原凭证与自然期限，长期占用仍409且不自动重放物理写入。跨项目复制、显式清单PATCH及项目删除保留NOWAIT；Member/owner/Ref/Gate锁不改变。CPU/线程/网络及permit等待不持业务锁，cleanup只取owner与Gate，避免逆序。读取始终联查当前project→Ref→ready owner，注册准确UUID目的，条件读取并完整SHA/大小/末端对象核验，真实关闭后才返回内容；未知/变化409，缺失404。原项目不存在不导致引用改指当前桶。

Web useGuideAssets改认证raw上传及完整内容读取，复用silentPost/silentGetBlob，不增客户端框架。PNG/JPEG/WebP/GIF使用Blob URL；SVG保留原字节，使用不继承应用源的 `data:image/svg+xml;base64,...` 图片地址，不把可执行SVG变成应用同源Blob文档。现有CSP已允许img-src data:。依据[MDN SVG图片限制](https://developer.mozilla.org/en-US/docs/Web/SVG/Guides/SVG_as_an_image)、[data URL的独立不透明源](https://developer.mozilla.org/en-US/docs/Web/URI/Reference/Schemes/data)和[File API Blob源规则](https://www.w3.org/TR/FileAPI/)，图片上下文的脚本限制不能替代直接打开文档的源隔离；API内容响应使用attachment及nosniff，不依赖其响应头自动传给Blob。真实浏览器以无害SVG脚本标记检查正文、放大图及打开图片地址的源隔离，不引入转码或净化依赖。

一个hook owner管理account/session/project/实例scope的AbortController、in-flight、缓存、全部Blob URL及SVG内存地址；scope退役后取消、统一revoke并清除地址引用，迟到结果不创建/返回可用地址，不发旧complete或新账号请求。token刷新延续session，换账号/重新登录更换resolver身份，使MarkdownView第二层缓存和放大图观察到变化。scope内刷新不立即释放仍被正文/放大图引用的旧地址，直到scope退役统一回收。MarkdownEditor异步预览需跟随imageScope/resolver变化，保留正文草稿及上传占位节点/撤销，不用整体remount抹草稿。设置页删除错误和上传toast同样隔离退役页面。

### 实施、验证和回退

超过8文件，按当前本地HEAD建立三个隔离树：领域/0228/严格存储和约束测试；Guide认证流程/路由/cleanup及API测试；Webhook/API/编辑器/三处消费者和相关测试。主树集成Project创建/清单PATCH/删除、seed清理扩展、周期注册、恢复映射/夹具、正式文档/生成合同和真实验收。新owner与引用必须同步进入隔离恢复，原键、SHA、元数据、状态、首次退休时间和引用关系保持；仅bucket/identity物理映射。新增周期任务使总数从71变为72，验证普通/maintenance Worker注册、实际队列和Beat。

验证严格迁移/受限恢复/禁止降级，五MIME及1/5MiB边界、同键异桶/损坏/缺失、pending与未知提交、两窗原Key/账号/当前可见性和自然期限、打开真实句柄与未知关闭。并发覆盖上传/删除/显式PATCH/复制/项目删除、陈旧修订不吞新图、最后引用与复制交错、来源删除后副本继续读、伪造JSON无法授权、ready无ref只退休及退休不可复挂。保留/更新test_guide_assets、projects_clone、project_templates和export_aap_json合同；Web覆盖hook scope、重复图去重/刷新/URL释放、MarkdownView/Editor及三入口。实际e2e必须更新markdown-authoring的旧三接口监听并验证表格、失败重试、取消、20次重复图和同项目换session；不能只跑新hook单测。

主树用已验证一次性test环境运行DB，实际18100/13100 HTTP/MinIO/Celery和浏览器验证上传、共享复制、删来源、坏内容恢复重试、URL退役及明暗/桌面/窄窗。离线演练先销毁合成源再向空目标恢复，覆盖三态、共享引用、无原项目/账号、未知legacy；不授予生产RPO/RTO。同步README/CHANGELOG、项目API、项目/工作台/入门/模板用户指南、存储架构与恢复部署、SDK项目通用字段兼容说明及coverage分类，不冒充新增一等SDK接口；生成OpenAPI/类型/路由索引，正常hooks、相关检查与diff通过。

应用0228并刷新人工3100/8100的API、两个Worker和Beat，保留原24对象、12源文件及业务；无图片历史记录也必须核对新增空表与修订初值。未请求发布版本/依赖/共享配置/镜像重建。回退保留0228与独立责任，旧客户端升级认证内容接口，不重新开放最终键PUT。一次性资源、浏览器、探针、测试/构建缓存和已集成子树按归属清理。完整E02继续执行，完整用途切换、其他媒体/RLE/Task消费者和E10真实框架资格仍独立推进。

## E02-B11 Outcome — 指南图片独立归属与项目共享引用

本地实现已提交至 `d276ebe94`；主集成提交为 `7d3c60adb`，领域、流程、恢复、Web、seed与正式文档分别通过隔离树集成。0228增加独立GuideAssetUpload、ProjectGuideAsset和清单修订；原请求凭证证明只扩展允许角色参数，评论附件默认权限保持。五种原字节图片经raw上传、条件创建和完整内容回读后发布，同事务关联当前项目清单；内容认证读取保存位置，不返回存储URL。旧三个合法接口明确409升级。显式清单PATCH使用CAS，只允许当前条目子集与重排；原历史重复、无key及未知条目保留清理和复制能力，不因JSON或键前缀获得读取权。

复制在全部backend网络检查后重新锁定源快照、可见性、正文、清单、修订及ready引用，创建独立目标引用。删图片、删项目或seed删除仅退役对应引用；仍有其它项目引用时图片继续可读。普通Worker新增周期任务，ready无引用先退休、后续才按保存位置物删，pending和未知历史保持。Web三个入口统一认证图片解析，按账号、会话、项目与实例隔离进行中请求和缓存；旧Blob在scope退役时全部释放。SVG原字节使用不透明data地址，编辑、源码、预览、表格及放大保持能力。

本地API九套JUnit按(classname,name)合并后为570个唯一用例，最终全部通过；此前一项自然到期测试夹具和三项锁竞争探针夹具已修正并以同名回归通过覆盖，不重复计数。Web99、SDK合同4、恢复单测30、Markdown实际E2E19与归属E2E2全部通过，共724个去重用例。实际浏览器首次暴露并行上传、autosave和预览对Project行的短时NOWAIT竞争；仅普通单项目路径改为事务内1秒有界等待并在末端重核期限，跨项目复制、显式PATCH、项目删除及其它锁保持原规则。真实双连接测试覆盖短等成功、等待期间过期拒绝和长等409零物理写入，最终19项Markdown E2E无跳过或flaky。

一次性18100/13100、MinIO与当前两类Worker完成七组HTTP检查：五MIME并发、中文空格及字面百分号、精确5MiB边界与非法请求零owner；旧合法接口409；未知规范键409且解除清单不物删；复制后删除原项目仍读全六图；同大小损坏409及修复重试、维护Gate暂停恢复；陈旧CAS拒绝后正常保留两图，真实GC准确删除另外四图。验收收尾公开删除目标项目，下一次实际GC删除余下两图并核对六个owner与引用均不存在。自建未知legacy仅按探针的精确键明确移除。seed清理遇到未知独立责任时正确409，未绕过保护，最终销毁整个已验证一次性mode。

命名浏览器检查项目设置的明暗主题、1440×1000与390×844，三图均加载且无页面横向溢出；真实放大与Escape、同账号重新登录及旧Blob全部撤销通过。另以标注员打开首页开工清单，原项目已删除的副本仍显示PNG、SVG与表格重复图片。独立打开原始SVG的实测结果为origin=null、应用存储blocked；其中脚本运行仍处于不透明源，应用marker保持。Workbench GuidePanel有单测回归；本次保留副本没有任务，实际入口显示无任务，不冒称完成其真实画布验收。控制台无应用异常，预期注入错误之外相关请求正常。

离线合成恢复先销毁源，再恢复空目标：159表、140行、49对象、9268字节、九桶，迁移头0228。五个指南owner与文件、四条引用、两个共享项目、已删除原项目/上传者、两个修订及一份未知legacy均保持；只映射bucket/identity，源与目标清理已独立核对。该结果仅为合成离线资格，不代表生产RPO/RTO、真实迁移目标或GPU/训练框架资格。

OpenAPI、类型与605路由索引生成通过，正式API/用户/架构/恢复文档和SDK coverage同步；类型检查、Web lint、CSS tokens、文档检查、正常hooks及diff检查通过，lint有四条既存warning。人工3100/8100已应用0228，受控刷新两Worker和单独监督Beat；两Worker均注册72任务，路由与实际GC派发正常，Beat周期300秒。迁移前后及测试清理后均核对11张业务表指纹、24对象完整SHA与12源文件保持，新表为空、修订初值正确。未重建镜像或改变共享配置、依赖与版本。

test/e2e进程停止，两个mode各自DB、专用Redis和九桶销毁后独立查验不存在；命名浏览器关闭，探针、日志、测试缓存及七棵已集成且干净的隔离树按归属清理。人工服务和工作树本地生成类型保留。完整E02继续active、完整用途切换关闭；下一片优先持久RLE，独立Task原件、已有Dataset位置的读取消费者、金字塔和视频派生分别收口。

## E02-B12-A Design — RLE 写入核验与取消收尾

基于干净HEAD `baade317e` 继续既有实施授权，先交付可独立合并的存储闭环修复。源码和只读审查确认：`_put_object_sync`把HEAD存在当成功，损坏同键对象可产生成功ref；`verify_upload`还会把全部ClientError视为缺失。裸to_thread取消后物理线程继续，调用者却可能已回滚预留并释放内容锁。本片只修这些当前配置下可触发的问题；完整B12仍须另行交付独立物理owner、保存位置读取与owner清理，不把本片视为用途切换就绪。

最小实现保持同步/异步store/load签名、内容寻址键、portable DTO、4MiB canonical/8MiB压缩/膨胀比限制、现有配额/引用事务锁和24小时GC。严格HEAD只将404/NoSuchKey/NotFound视为缺失；新建带IfNoneMatch="\*"，只有并发412转为读取赢家，409及其它错误原样失败。新对象、现存对象和412赢家均经固定长度/ETag/有效VersionId、条件GET、有界分段完整读取、真实关闭及当前键末端HEAD核验后才成功。失败不覆盖或补偿删除。条件写语义依据[Amazon S3官方说明](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)，不将HEAD存在或PUT应答单独当完整内容证明。

JSON必须与本次canonical原字节一致；gzip完整有界解压后比对同一canonical字节。历史gzip带mtime，同内容压缩字节可不同，不能以本次压缩字节强制拒绝旧对象，也不修改旧对象。载入仍校验canonical SHA、尺寸、runs、bytes与规范键。两个编码都核对实际响应身份及末端当前对象，不用历史VersionId HEAD冒充当前键检查。

异步store/load复用storage_access.finish与close_registered，持有shield线程任务；重复取消或外层超时均先等待线程和句柄收尾，再抛原中断，迟到成功不变成调用成功，迟到关闭未知保留unsettled标记。依据[Python asyncio取消说明](https://docs.python.org/3.12/library/asyncio-task.html#shielding-from-cancellation)保留任务强引用并显式drain。观测仍只记录一次外层操作，取消不记成功，不增加高基数标签、重试层或新超时参数。该修复延长现有内容锁至线程结束，但不冒充解决所有writer与引用提交之间的间隔或永久未知句柄责任；这些随完整owner协议交付。

预计八文件：存储服务、三个低层测试文件、segmentation在线闭环测试、Mask用户指南、CHANGELOG与本计划。测试provider必须真实实现条件PUT/GET、身份元数据与关闭，不在生产代码识别Mock放宽。先用损坏同键和取消线程红测证实；随后覆盖JSON/gzip历史mtime、条件竞争、权限错误、损坏回读、VersionId/null、末端替换、短读/截断/超界/关闭失败、重复取消和超时、指标单次计数。纯单测使用当前虚拟环境；涉及迁移/在线闭环只用验证过的一次性test mode。实际本机MinIO验证条件创建与历史gzip复用，人工API/Worker受控刷新并保持24对象/12文件。无UI/DTO/依赖/配置/镜像/版本变化；正常hooks、相关回归与diff通过后记录Outcome并清理任务产物。

## E02-B12-A Outcome — RLE 条件创建、完整核验与线程收尾

八文件实现保持既有内容键、DTO、同步/异步入口、gzip兼容和24小时GC。严格HEAD不再吞权限/服务异常；缺失才条件创建，412赢家也必须完整核验。条件GET固定对象身份，有界读至EOF、真实关闭，再检查当前键；新建、现存或并发赢家损坏均拒绝，未覆盖或补偿删除。历史gzip按完整canonical内容比较，保留原压缩字节。GET缺失、已删除版本和条件冲突继续作为内容错误，保留API的409及缺失分类，不误报为存储503。异步操作以强引用shield并drain，重复取消与超时等待线程收尾后仍抛中断，迟到关闭未知保留unsettled，取消不计成功。

改前五个红测全部失败，准确复现两个编码损坏同键被接受、无条件创建/无回读、历史gzip未核验及取消提前释放锁。改后三套低层66项通过，覆盖条件竞争、403/409、写后损坏、真实VersionId/null、末端替换、短读/截断/超界、读取/关闭失败、阻塞PUT、读取/关闭期间重复取消和外部超时。一次性test中的人工Mask API、portable导入导出、预测内容、开关/配额、GC及segmentation训练/人工审核闭环六套81项通过；合计147项无失败或跳过。训练闭环使用确定性transport/ML替身，不代表真实框架或GPU资格。

真实本机MinIO完成四组检查：JSON新建/复用完整读回，mtime=1的历史gzip复用后原字节保持，损坏JSON/gzip拒绝且不覆盖，以及强制两个初次HEAD均缺失的并发创建。实际PUT结果为一个成功、一个PreconditionFailed，两调用均完整读取同一正确赢家。五个探针对象精确移除，之前对象集合保持。未改UI，不重复浏览器矩阵。CHANGELOG与Mask用户指南同步，Ruff/format、文档可读性200页、frontmatter199页、正常hooks与diff检查通过。

人工3100/8100在确认两Worker无active/reserved/scheduled及相关队列为空后，通过原拥有服务受控刷新；新PID、当前源码、两Worker各72任务、队列与实际cleanup派发、Beat及10项doctor均正常。再次完整核对11张业务表指纹、24对象SHA和12源文件保持，迁移头仍0228，无新迁移。一次性test停止并销毁，DB、Redis及九桶独立查验不存在；探针、日志、测试缓存按归属清理，保留人工服务及本地类型。没有重建镜像、发布版本、修改共享配置或安装依赖。

本片只完成写入核验与线程收尾，完整E02仍active、完整用途切换仍关闭。下一片继续持久RLE独立归属：统一store/load的全部调用者及同步loader、物理写入前耐久pending、保存位置和实际内容回执、全部权威引用与既有保留期下的owner清理、未知历史及未知收尾保留、Task删除和恢复映射。不能只接管人工上传，不能把RasterMaskUpload的Task级配额账当独立物理owner，也不重复新建已有Dataset原件归属。

## E02-B12-B Design — 持久 RLE 独立候选与业务发布保护

基于干净HEAD `dd33fd62e`，延续整套实施和提交授权，三路只读审查已核对真实调用。本片完整接管持久RLE的store/load及GC，不将新增几个定位字段当作完成。现有RasterMaskUpload只保留Task级配额，不能承担物理归属；portable CocoRleMaskRef禁止额外字段，其object_key继续是canonical SHA加编码的逻辑键。实际对象改用 `raster-masks/objects/{ownerUUID}.json[.gz]`，由独立RasterMaskObject保存严格位置。新候选无User/Project/Task级联FK；未知旧逻辑键、JSON与字节不扫描认领，纯读取零I/O明确409，条件304也必须先核验归属与门禁。

0229新增raster_mask_objects和raster_mask_publications。Object固定UUID、logical_key、canonical_sha256/canonical_size_bytes、height/width/runs、identity或gzip编码、stored_sha256/stored_size_bytes、九字段annotations storage_ref和created_at；JSON两套SHA/大小必须相等，gzip分别保存。logical_key必须与canonical SHA、分片和编码后缀严格一致，物理键与UUID和编码严格一致。全局部分唯一为logical_key WHERE state=ready，不能按桶分组。状态仅pending→ready/retired、ready→retired；published_at首次ready设置且不可改，last_used_at仅成功store复用单调前进，retired_at首次退休后不可改。pending物写前独立提交；未知提交、取消、失败与未知关闭保留，不续写旧pending、不反向复活。物理失败后新调用可用另一UUID候选，旧候选仍保留责任。

已存在ready必须沿保存桶完整核验canonical与实际存储回执，然后重核同一不可变快照/ready/Gate并更新last_used_at。无ready时分配独立候选，条件PUT并完整回读、真实关闭后，在独立的 `aap:rle-publish:{logical_key}` 短锁下发布ready；该锁不与调用者已持有的旧内容锁或Task锁重名。竞争已有赢家时先释锁、完整核验赢家，再短锁重核；只在明确成功后退休本次已完成候选，不补偿物删。若同逻辑键后续合法重建，旧portable引用可解析到内容事实完全相同的新ready；本协议不承诺旧代次引用永久拒读。没有新规范内容写入时，旧未知引用不能借键或当前桶获得owner。

独立连接绝不回取调用者Task、配额或内容advisory锁，也不提交调用者事务。使用当前caller bind的独立会话；无caller的读取使用本次操作自有engine并dispose，不跨Worker事件循环复用asyncpg池。默认同步store/load同样走独立归属，不保留当前桶后门；同步入口只在无活动事件循环的线程运行，有活动loop时明确要求async入口，训练物化与样本评估的现有线程调用保持。原有训练存储接管的显式冻结副本loader继续保留。低层纯物理I/O与归属流程分层，测试显式验证各层，不在生产代码识别Mock或省略owner校验。

仅靠24小时宽限不能保护长视频逐帧物化至最终staged_result之间的间隙，因此增加耐久Publication。它只FK独立Object、保存不可变id/created_at和准入Gate修订，不依赖业务行。所有实际async写入传入caller db，物理I/O前独立提交guard；caller db.info仅跟踪本次精确guard ID，不作为存储真值。普通进度commit不得清除guard。最终发布引用或上传预留时，在原业务事务重核同ready、内容事实和Gate，再DELETE本caller/key的准确guard，与业务引用commit原子结清；回滚或应答未知留下guard。竞争赢家时候选guard转为赢家的新guard必须与候选退休同一独立事务，不能失去保护窗口。无caller的独立内容写入依赖首次发布/最后成功使用后的24小时宽限，后续业务引用仍须通过统一验证；生产async writer不得遗漏caller。未知Publication不按年龄删除。

保留现有内容锁用于已知引用的提交校验，改为有冲突明确409的try advisory，避免持业务行锁继续等待多键环。预测AAP/COCO导入在首次store前NOWAIT锁Task，修正现存内容→Task与人工Task→内容反序。QC新region通过guard原子发布到issue，避免给每条扫描积累内容→issue长锁；普通Tracker的guard跨进度commit保留，到取消保留候选或最终staged提交才结清。Annotation导入、预测、AI采纳、转换、修复、区域视频审核、controlled segmentation和两个seed均接统一store/helper，不改变其业务事务或原权限。人工upload最终把已验证guard与未绑定RasterMaskUpload预留一起提交，后续绑定仍按原quota语义。

GC不再列当前桶前缀，只筛独立owner。保留训练非ready快照、部署trial、活动Annotation、Prediction、可用Tracker候选、accept/revision/QC/repair及期限，并补当前可用MaskReviewScope.region_mask_ref和新鲜未绑定上传预留。候选扫描只用于筛选；在内容锁→发布锁→owner的同序短事务中重新查全部引用、Publication和准确UUID read/write/delete许可。ready超过published_at/last_used_at后的24小时且无保活者先提交retired屏障，本轮结束；后续才沿保存位置物删retired，真实收尾与确认后删除记录。退休对象不可重挂，新同内容候选不复用旧UUID物理键。pending、未知关闭/操作、未知旧键和失败删除保持独立责任；dry_run不推进状态。

本片超过八文件，按当前本地HEAD建三棵隔离树：领域/0229/严格存储及约束测试；独立store/load/Publication/owner GC流程及生命周期测试；全部writer接入、预测锁顺序/304与对应回归。主树集成统一入口与引用查询、恢复白名单/夹具、隔离测试的独立记录清理、正式API/用户/架构/运维文档及真实验收。恢复仅映射bucket/storage_identity，保留UUID物理键、逻辑键、两套回执、状态、首次时间、guard和所有冻结JSON；未知桶拒绝。新增表不新增Worker任务或调度，仍为72任务；不新增进程、凭据、依赖、环境变量、部署目标或镜像重建。

验证迁移/严格类型/不可变/受限恢复与降级保护；JSON/gzip、同逻辑异桶陷阱、未知零I/O、缺失/损坏/末端替换、竞争赢家/失败pending和退休后新UUID；caller持Task/内容锁时独立分配、进度commit后guard保持、最终业务commit原子结清、rollback/未知应答及未知关闭保留；当前复核scope保活、全部十类既有来源/期限、24小时边界、guard/真实打开句柄与GC交错、Task删除后owner存续、旧GC不触碰新代次。真实HTTP/MinIO、当前Worker及来源销毁后的离线恢复必须覆盖完整闭环；纯单测不替代实际归属。保留人工3100/8100、24对象/12源文件及业务指纹，应用0229并受控刷新所属Worker。完整E02与其它消费者继续执行，完整用途切换仍关闭，生产迁移、RPO/RTO与真实训练框架资格独立核验。

## E02-B12-B Outcome — 持久 RLE 归属、发布与分轮回收

领域与0229提交 `eac8b01be`、生命周期 `f90013350`、调用者接入 `673fafb65` 和最终集成 `ed40b31b3` 完成既定闭环。Object独立保存UUID位置、规范内容与实际字节双回执；Publication在物理I/O前提交，跨进度commit保留，仅与最终业务引用或匿名上传预留原子结清。未知pending、提交/关闭不明和未知发布记录不自动到期。竞争写入完整核验赢家后才转移保护与退休已完成候选；历史gzip时间头及legacy gzip标记保持兼容。同步训练/评估入口重复建立并关闭自有事件循环与连接，不按当前桶猜读。

统一store/load与所有实际async writer接入caller事务。内容锁使用非阻塞申请，预测导入先NOWAIT锁Task；QC在issue upsert后绑定，Tracker进度提交不解绑。bind只锁精确Publication，再复读owner和Gate，避免同一批处理中先绑定后复用相同内容时独立连接与自身owner SHARE锁冲突。存储和门禁错误保留固定409/503及重试信息，转换、像素运算和多文件导入不改成输入422或跳过。失败导入先回滚业务，再单独提交Job失败记录，两条真实数据库回归证明原删除被恢复、新插入不存在。

GC只枚举独立owner，补充当前有效Annotation版本的区域审阅scope和24小时内未认领上传保活；job/issue为空仍有效。成功store或复用延长24小时窗口，读取不延长。首轮只提交retired，后续才核验并删除保存UUID；持久guard、准确UUID未收尾操作、pending和未知旧键保持。旧退休UUID不因新ready承接相同逻辑引用而无限保留；尝试时间保证有限批次公平，dry-run不修改状态或期限。超过24小时的未认领预留释放额度，不据此删除未知文件。

最终675个不重复API、服务及数据库用例通过，另37个恢复纯单元通过，无剩余失败或跳过。覆盖97个严格存储纯测、68个迁移/约束测试、独立事务与竞争、进度/回滚/未知commit、重复取消和真实关闭、全部既有Mask业务和训练合成闭环。首轮失败分别是测试夹具在触及数据库前拒绝非法编码，以及一条既有Tracker测试硬编码Redis6379；修正夹具和指向当前隔离Redis后均复验通过。训练/ML替身不构成真实框架或GPU资格。

真实TCP API与本机MinIO验证JSON上传/复用、匿名预留结清、历史mtime=1的gzip原字节保持、保存引用后的200/304、损坏对象命中ETag仍409、未知legacy命中ETag仍409。删除源Task后owner保持，两次同步线程读取正常；两轮回收只删除已知UUID，legacy字节保持。另用一次性真实Celery worker验证72任务注册、dry-run不改ready、第一轮退休且字节仍在、第二轮删除原UUID及记录。HTTP探针和Worker均关闭，精确对象及合成业务清理完成；探针用户的审计外键清理遵循既有事务局部审计豁免，不改变生产约束。

离线合成恢复先销毁随机源，再恢复161表、152行、九桶56对象/9680字节，迁移头0229。七个RLE owner、六份已存在字节、两个发布guard、一个未知写permit、空pending、历史gzip、同逻辑新旧代次、已删除Task及未知legacy均对账；仅映射bucket/identity，原键、双回执、时间、状态与冻结内容保持。源与目标资源已清理；该结果仅为本地合成资格，不授予生产RPO/RTO或真实迁移目标资格。

API、用户、架构、恢复说明、README与CHANGELOG同步；OpenAPI快照检查、文档构建、Ruff/format、正常hooks及完整提交范围diff检查通过。本片未改UI，不重复浏览器视觉矩阵。人工3100/8100已应用0229，在两Worker及队列空闲时由原拥有服务刷新；两Worker各72任务、实际RLE只读清理派发、指南清理、Beat与doctor正常，RLE原UTC03:20调度保持。11张业务表指纹、24对象完整SHA与12源文件保持，新表为空。没有重建镜像、安装依赖或修改共享配置与版本。

一次性test及恢复源/目标三个命名空间的数据库、Redis、合计27桶已独立查验不存在；五棵干净且patch等价的隔离树和分支移除，任务产物清理，保留人工服务、工作树身份和本地生成类型。完整E02仍active、完整用途切换关闭；后续推进独立Task原件、已有Dataset位置消费者、金字塔与视频派生，不重复建立已有Dataset原件归属。

## E02-B13 Design — Task 原件与既有 Dataset 位置消费者

基于 `3e70255ef` 的三路只读盘点，独立Task生产写入只有 `/files/upload-init` 和 `upload-complete`；ZIP、目录与连接导入仍归DatasetItem。旧直传直接签最终annotations键，完成仅HEAD，无完整SHA和耐久写入责任；同时缺显式项目管理授权。NULL dataset_item_id不能直接视为独立原件：历史点云可能通过TaskDatasetItemLink关联primary_lidar。DatasetAsset/Location继续承担已有Dataset原件归属，不新建重复owner。

按两个可独立验证的片段实施，但不把第一片视为完整B13或用途切换就绪。B13-A先关闭独立Task直传的最终键覆写和归属缺口；B13-B统一所有Task/Dataset原件消费者，处理固定位置、未知历史和大文件/Range。后者覆盖DTO/file-url、视频/多相机manifest、ML与Worker、金字塔源、导出/训练/AL/样本源；不能只替换几个helper。64MiB图片snapshot不能直接套用于视频与点云。历史原件的显式接管需精确端点/桶/键、全字节核验、当前授权和门禁，不依据file_path、ETag或当前同名桶自动认领。当前全部用途切换保持关闭。

B13-A复用ManagedOriginalCreation独立事务、条件multipart创建、完整回读、取消drain与未知收尾记录。显式新增owner_kind=dataset/task，Dataset记录继续保存原dataset_id；Task记录dataset_id为空，保存独立project_id/task_id，禁止伪造Dataset。历史Creation不改写；原终态不可变、独立记录不级联、不授予独占物删权。Task最终键采用独立受保护命名空间和随机creation UUID，浏览器只获import暂存键PUT。重复/仍有效的暂存签名不能覆盖已接受最终对象。条件写依据既有Amazon S3官方协议，使用现有writer而不添加重试层或第二套物理写入器。

新TaskOriginal仅保存独立已核验原件的当前位置和内容/写入回执，引用Creation证据而不FK业务Task/Project/User。Task通过可空original_id与之关联；既有行保持空。当前物理位置与不可变Creation历史分开，受限离线恢复只映射当前位置的bucket/identity；禁止借修改Creation伪造目标环境写入。创建失败、发布回滚或源Task/Project删除均不抹除Creation责任；已完成但未绑定owner保留。没有新增原件GC或自动接管历史。

init与complete要求tasks:write和当前项目管理身份，复用现有原请求JWT/API key证明与短NOWAIT授权锁。init在分配和业务提交/签暂存URL前复核；complete由原上传发起者完成，重核项目、Task、原凭证、当前可见性和Gate。I/O期间释放业务锁；末端以短锁重核同一Task快照与配置，再原子关联owner和pending状态。并发完成、删除、权限/凭证撤销或Gate变化拒绝发布，已写候选保留责任；不补偿删除。未知旧uploading没有Creation则明确409，不HEAD当前桶猜测接受。幂等成功仅允许同一已验证Creation/owner，不能因Task状态变化二次写入。

本片超过八文件，分三个隔离树：Creation显式作用域与0230领域迁移；TaskOriginal领域/固定回执绑定与独立服务；恢复映射/夹具与测试。主树负责认证路由和原凭证复核、生产签名器保护、API合同/SDK分类、文档及集成验收。先完成类型、约束、不可变/禁止降级、旧Dataset回归；再覆盖暂存重放、最终同键冲突、完整回读损坏、未知收尾、竞争完成与删除/权限撤销、业务回滚后责任存续、点云关联排除和恢复映射。真实TCP API/MinIO必须验证写入回执与源删除后的责任，离线恢复先销毁源再验证当前位置映射与历史不变。

主树使用已验证一次性test环境运行DB，不触及日常数据库。人工3100/8100在业务空闲后应用新增迁移并刷新所属Worker，核对72任务、两Worker/Beat和原24对象、12源文件及11业务表指纹。无UI变更不重复视觉矩阵；不更改共享env、依赖、镜像、部署目标和版本。正式文档、CHANGELOG、API快照及相关检查随实现提交；清理测试资源和干净已集成隔离树。回退保留新增归属/历史证据，禁止重新开放最终键PUT。

## E02-B13-A Outcome — 独立 Task 原件封存与耐久归属

主集成 `66c51039f`，创建记录、原件领域和恢复分别集成于 `94c03ad4c`、`399984a7a`、`c136b5f91`。0230显式扩展Creation作用域，旧Dataset字段和终态历史保持，Task不伪造Dataset；0231增加无业务级联的TaskOriginal及Task.original_id。原件先独立提交已验证位置/内容/源写回执，再与Task状态同事务关联；回滚、未知提交或删除业务行不抹除责任。Task与DatasetLink双向锁定，NULL直接外键的多文件点云不能被误认。

两个低层上传接口保留请求/返回形状，增加名称/类型边界、tasks:write、当前项目管理权限与原发起者检查。暂存区PUT后复用既有条件multipart封存、完整回读、真实关闭及取消收尾，单文件5GiB。末端重核原请求JWT/Key、Task/Project和Gate，释放业务锁后才进行物理I/O；提交等待期间自然到期也不返回URL或成功。重复完成不重写最终对象或重置后续业务状态；未知旧uploading零物理I/O返回409。最终UUID命名空间禁止客户端PUT签名，不新增原件GC或历史自动接管。

223个不同本地API、服务及DB用例全部通过，另59个恢复纯单元通过，无失败或跳过。覆盖作用域/迁移/不可变/受限恢复与禁止降级、旧Dataset创建回归、JSON以外的通用字节/视频/空文件、暂存重放、同SHA或异内容最终冲突、坏内容/未知许可与重复取消、发布回滚/未知commit、Task删除、Key撤销/轮换/scope/期限变化、提交自然到期及DatasetLink并发。当前项目DELETE和真实seed namespace清理均证明业务行消失后owner、Creation与原件保留；未完成暂存责任也保留。后者依赖最终销毁整个一次性test环境清理，不绕过历史触发器。

真实TCP API和本机MinIO六组验收通过：封存并保存完整回执、暂存URL重放后最终SHA不变、源Task删除后原件保持、最终同键冲突保留对方字节、未知旧上传不认领或删除、已删除Task拒绝完成且保留allocation，以及最终键签PUT拒绝。首轮探针遗漏既有浏览器相对代理地址，按当前代理的origin映射处理后全部通过，生产签名方式未改变。探针与HTTP服务均关闭。

离线恢复先销毁随机源，再向空目标恢复162表160行、九桶64对象/10331字节，迁移头0231。包含两个独立Task原件、四条Task Creation（当前关联、源Task删除、已验证未绑定、未完成）及未知legacy Task；仅当前bucket/identity映射，源Creation、UUID键、SHA/大小、源ETag/VersionId及时间保持。目标当前HEAD/完整SHA核对通过，不用源VersionId选择目标版本；不宣称B13-B业务读取已交付或生产RPO/RTO合格。

README、CHANGELOG、API/用户/架构/存储桶和部署恢复说明同步，执行队列补齐B11/B12既有Outcome。OpenAPI仅增加输入长度边界，快照检查及本地类型生成、文档build、Ruff/format、正常hooks和提交范围diff检查通过。无UI变更，不重复视觉矩阵；SDK的files内部接口排除分类保持，不新增一等SDK承诺。

人工3100/8100已应用0230/0231并由原拥有服务刷新。迁移前保留旧API服务子进程，验证后恢复reloader；启动收敛后doctor、两个Worker各72任务/准确队列、实际Guide清理与RLE dry-run、Beat五分钟及原UTC03:20调度正常。11业务表指纹、24对象完整SHA及12源文件保持，新增Task原件/Task作用域创建记录为空，既有Task指针保持NULL。没有重建镜像、安装依赖或修改共享配置/版本。

三个一次性命名空间的数据库、Redis及合计27桶已独立查验不存在；三棵干净且patch等价的隔离树和分支移除，任务产物清理，人工服务及本地类型保留。完整E02和B13仍在执行，完整用途切换关闭。下一片B13-B先统一TaskOriginal、直接DatasetItem和primary_lidar的关系定位，接已有认证content/identity；随后覆盖媒体/大文件、推理、导出和冻结来源，不能按当前桶猜读或重复建立Dataset原件owner。

## E02-B13-B1 Design — 原件关系解析与完整字节暂存

基于干净HEAD `702cb8bf5`，三路审查确认当前content/quality仍按现行datasets桶过滤，裸GET没有维护许可；reference把直接外键当作唯一Task关联。B13-B继续覆盖全部原件消费者，本片先交付统一关系/固定位置、可供后续媒体使用的完整二进制暂存，以及现有认证图片/质检入口。没有接入的DTO/匿名file-url、ML/ROI、视频/点云/相机、金字塔、导出、训练/评估/AL冻结来源继续列为B13-B剩余项，不因新增helper或本片图片验收宣称完成。

新增不提交调用者事务的original_sources解析器，返回冻结的关系与内容/位置事实。TaskOriginal要求Task/Project/路径精确一致且无任何Dataset关联；Dataset直接FK和primary_lidar同一Item接受，不同409；无direct但有primary可读，仅camera、无owner或未知位置409且零对象I/O。Dataset继续使用verified SHA/大小和唯一present managed_original位置，允许没有Creation的已核验历史；unverified回填、ETag、当前同名桶不升级为证据。多合格managed位置明确歧义，不任取一条；original_cache/preview不冒充原件。平台Location必须启用、配置可解释并与实际客户端端点一致，读取不依赖当前用途桶、不调用Creation.\_validate；可选Creation只保留历史来源，恢复后的桶/版本不要求等于源写入回执。

reference内部复用同一Task关系判断并继续原task_authorizer及真实路径检查，不改变已有context字典/冻结训练证明，也不把Task读取改成数据集管理权限。末端按User/Key→Project/Member→Batch→Task→准确link/Item/Asset/Location短NOWAIT共享锁刷新实际权限与关系；解析器不自行commit。Task SHARE与既有link写触发器共同挡新增/更新，现有link本身也取共享锁以挡删除。业务锁不跨物理I/O，初始、许可取得后和交付前分别复核冻结源与Gate代次；冲突明确409，拒绝等待成环。

纯物理managed_original_reader接收保存endpoint/bucket/key、SHA/精确大小及显式max_bytes/timeout，使用当前无VersionId HEAD、本次观察的IfMatch/有效目标VersionId GET、分块落本地TemporaryFile、完整SHA/大小和EOF、真实Body/client关闭、当前无VersionId末端HEAD。严格核HTTP200、元数据和条件能力；不支持条件的provider失败，不静默降级。源ETag/VersionId不作目标版本选择器。[S3 GetObject官方合同](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/get_object.html)只提供条件传输，完整SHA仍由本地核验。四个二进制暂存槽、文件明确close、线程强引用/shield/drain和迟到结果关闭保留真实资源责任；未知关闭保留durable许可，不误记已排空。临时文件遵循[Python自动清理语义](https://docs.python.org/3.12/library/tempfile.html)，不新增持久副本表、缓存或依赖。

既有content_snapshot保持PNG/JPEG/WebP、64MiB/32M像素和质量宽容规则，增加直接接管已核验文件的图片适配，避免二次下载或整包内存。二进制层不继承图片大小/10秒/15秒预算，后续大文件消费者显式给出自身预算；Range仍维持当前图片接口416，不能用片段摘要代替完整证明。取消必须等待SDK/解码线程和句柄真实收尾，文件未交付时释放本次槽；Response结束仍关闭文件。依据[asyncio取消合同](https://docs.python.org/3.12/library/asyncio-task.html#shielding-from-cancellation)保留任务强引用并drain，不增加重试层。

API继续既有content与content-identity URI，支持独立Task图片和primary关联；identity显式区分dataset_asset/task_original，Dataset的asset_id保持，Task使用独立task_original_id且asset_id为空，禁止把TaskOriginal UUID伪称DatasetAsset。初始/末端复核原JWT或API Key、自然期限和当前可见性，包含viewer与当前Batch状态，不沿用上传的管理者限制。metadata identity保持original_availability=unchecked，不探测对象或声称在线。quality使用相同Dataset定位和严格文件，保留数据集管理/Worker原权限及冻结asset/SHA/size，reference仍走原来源/缓存合同。

本片超过八文件及三个组件，建立三棵当前HEAD隔离树：关系/位置解析与reference关系测试；纯二进制reader、图片适配及严格provider/取消测试；新内容入口回归与恢复夹具。主树负责原请求/当前权限短锁、dataset_content与files接入、质检及旧合同回归、正式文档/生成类型/真实HTTP和Worker验收。流程为 `Task/Item关系+权限 -> 冻结源 -> 释放业务锁 -> durable许可/重核 -> 完整二进制暂存 -> 图片适配 -> 当前关系/凭证/Gate短锁重核 -> Response关闭`，没有新环境变量、外部凭据、服务、迁移或镜像重建。

验证明确覆盖无Creation的已核验历史、旧未核验零I/O、同键异桶陷阱、managed多位置/缓存角色、TaskOriginal及direct/primary同一/冲突/link-only、reference普通标注员、读取期间关联/权限/Batch/Key/Gate改变、条件忽略/206/错metadata、截断/超长/损坏/末端替换/真实关闭、重复取消与未知收尾/槽释放。真实TCP/MinIO核对固定桶内容及拒读，源销毁后的离线恢复通过实际Task/Item内容读取证明目标当前观察有效且历史不变。只用已验证一次性test DB，人工3100/8100、两Worker/Beat、24对象/12文件和11业务指纹保持；正常hooks、相关检查、docs build与diff通过后提交Outcome并按归属清理。回退代码不改既有归属/历史，不能恢复当前桶猜读后门；完整用途切换仍关闭。

## E02-B13-B2-A1 Design — 认证通用原件下载基础

浏览器盘点基于 `a83f6e997`：托管图片还接受 BMP/TIFF/GIF/SVG，点云完整解析 PCD，相机有面板、CPU/GPU 上色和邻帧预取；不能统一套用 B1 的单帧 JPEG/PNG/WebP、64MiB 图片解码合同。本片先增加可独立验证的认证完整二进制接口，不替换尚未适配 Bearer 的 DTO、原生标签或签名 URL；这些旧消费者仍是 B2-A2/B2-B 的明确缺口，不能据此宣布浏览器迁移或完整 B13 完成。

新增 Task 主原件、精确 TaskDatasetItemLink 相机原件及 DatasetItem 的身份/完整下载接口。身份包含 owner/content UUID、SHA、精确大小、媒体类别和平台相对下载路径，标明位置可用性尚未探测，不暴露端点、桶或对象键；身份查询零物理 I/O。下载必须提交同一 content UUID 和 SHA。Task 使用当前任务可见性，相机要求该 Task 下精确 link UUID、camera\_ 角色与 image Item，不接受任意 Item；DatasetItem 要求当前 Dataset 管理权限及路径 dataset_id 一致。原地引用继续使用既有认证图片合同，新通用接口明确拒绝，不扩大引用媒体能力。

```text
Authenticated routes -> current credentials + Task/Item/link source snapshot
                     -> durable Location admission -> strict binary capture
                     -> credentials/relation/source/Gate recheck -> owned response
```

复用冻结来源、保存位置、当前目标条件读取和完整 SHA/大小核验，不按当前用途桶猜读，不依赖历史 Creation 版本选择目标。User/Key、Project/Member、Batch/Task、link/Item/Asset/Location/Gate 的短锁在对象 I/O 前释放；许可后与交付前重核原请求凭证、自然期限、同一关系与 Gate。相机冻结全部 Task 关系及精确链接，允许主原件缺失时单独解析合法相机，但拒绝独立 owner 与 Dataset 链接冲突。

通用下载单文件上限 512MiB、独立两槽，最多 1GiB 已声明大小的同时暂存，额度覆盖完整复制、响应和真实关闭；B1 原四槽与图片合同保持。复制与响应分别使用 120 秒合作式期限；取消/超时必须等待实际线程及句柄收尾，不把期限描述为硬墙钟保证。响应为 application/octet-stream、attachment、no-store、nosniff，提供准确 Content-Length/SHA/ETag，拒绝 Range，不以 304 或 HEAD 绕过核验；512MiB 以上明确 413，原生长视频准备、能力凭据、Range/seek 与临时表示复用由 B2-B 专门处理。无新增配置、依赖、服务、凭据或迁移。

本片预计超过八文件，分离通用 reader/响应、关系/授权编排与主树路由/文档/集成验证。验证覆盖保存桶同键陷阱、非图片字节/空文件、TaskOriginal/direct/primary/camera、路径和 link 越权、旧 owner/SHA、未知历史、读取间凭证/关系/Gate 变化、两槽隔离、超限零 I/O、取消/发送失败/真实关闭与原图片回归。使用已验证一次性 test 命名空间及真实 TCP/MinIO；本片没有 UI 修改，不重复视觉矩阵。人工环境和原业务/原件保持，清理精确测试资源。回退只撤销新增入口，不删除原件责任或重开旧上传旁路。

## E02-B13-B1 Outcome — 原件解析与认证图片读取

主集成 `15df36577`，关系解析 `79727fe0e`，物理reader与图片适配 `90841c746`，入口/恢复验收 `798fe4cae`，引用许可回归 `ecf10cd8a`；旧RLE夹具修正 `f6f81ed33`、`862dbada6`。没有新增迁移、依赖、环境变量或镜像重建，迁移头保持0231。

original_sources返回冻结关系、核验身份和保存位置，统一独立TaskOriginal、直接DatasetItem及primary_lidar。同一Item可兼容两种关联，不同Item明确冲突；未知原件、多present托管位置、未核验内容和不明位置不进入物理读取。历史verified Dataset原件无需伪造Creation；缓存和预览不作为managed原件候选。reference复用同一关系判断，保留原授权回调、路径核对及context字典合同。

managed_original_reader从目标当前HEAD获取本次条件，核对错误If-Match确实失败、HTTP200、精确大小、完整SHA与EOF、Body真实关闭、最终当前HEAD及独立client关闭。源写回执的VersionId不选择恢复目标版本。四个二进制槽、临时文件移交、重复取消和迟到结果关闭经过验证；未知关闭保留未收尾许可。图片适配接管同一文件，不二次下载或复制，也不释放未申请的旧图片槽。超过64MiB的非图片纯读取用例通过，现有图片入口仍保留64MiB/32M像素和Range416；不宣称视频/点云已接入。

files的content/identity支持独立Task图片并显式返回owner_kind及互斥的asset_id/task_original_id；identity零对象I/O，availability仍为unchecked。托管图片及质量检查按保存桶/键读取，原JWT/Key、当前账号、Project/Member/Batch/Task、完整源与Gate在初始、取得许可后和交付前复核，业务锁不跨物理I/O。集成审查补齐reference来源和缓存命中的许可后原凭证复核、完整context比较及末端Gate代次锁；撤权时零物理读取，缓存不被误记损坏或回退读源，lease/permit正常退出。

最终426个不同本地用例通过：主树230个API/服务/DB及相关回归，隔离树135个二进制/图片纯测和61个恢复纯测。两条纯fetch-script另以无conftest运行，确认窄RLE前置夹具不引入数据库依赖，未重复计数。首轮全组四项失败中，两项来自TCP探针业务数据与全库计数断言混用，销毁并重建一次性namespace后通过；另外两项旧RLE夹具改用生产异步owner/publication合同，并在业务事务之前独立提交静态绑定，保留原包断言及既有配置，只重跑失败用例。没有放宽生产owner或读取校验。

真实TCP/MinIO八组检查通过：初始化/HTTP PUT/封存/读回、Dataset与TaskOriginal保存桶陷阱、符合既有批次规则的观察者读取、无关账号拒绝、图片Range拒绝、primary-only关联及同大小损坏拒绝。探针最初的无批次观察者访问按既有规则被拒绝，补齐可见未分派批次后沿用同一已封存原件完成检查，未修改可见性规则。另用当前checkout实际Celery Worker完成一轮托管图片质量检测，processed=1/1、bytes_verified=true、96×64、无质量问题。

离线演练先销毁随机源，再向空目标恢复162表160行、九桶64对象/10331字节，头0231。目标实际调用TaskOriginal和Dataset任务图片读取，临时把当前两个原件用途桶设为import陷阱后仍读回保存位置；源Creation、SHA/大小、源ETag/VersionId及未绑定/未完成责任保持，历史Creation不能自动接管目标。本结果只证明本机合成恢复，不声明生产RPO/RTO或全部原件消费者合格。

正式文档同步到README、CHANGELOG Unreleased、`docs-site/api/guides/verified-content.md`、`tasks-and-annotations.md`、`docs-site/dev/concepts/storage-connections.md`、`docs-site/user-guide/datasets/import-images.md`、`datasets/index.md`及`docs-site/ops/deploy/storage-recovery.md`。OpenAPI快照、本地生成类型、Web类型检查、docs build、Ruff/format、正常hooks及最终diff检查通过。无UI样式变更，不重复视觉矩阵；大媒体的真实浏览器播放/seek另行验收。

人工3100/8100、两类Worker和独立监督Beat恢复就绪；每Worker仍注册72项任务、队列正确，实际Guide清理与RLE dry-run通过，Beat来源/专用broker/九桶归属及UTC调度复核。原24对象、12源文件和11业务指纹不变。三个一次性namespace的数据库、Redis及27桶均已独立查验不存在；五棵干净且补丁等价的隔离树及分支移除，任务探针、构建和测试中间产物按归属清理，人工环境及本地生成类型保留。

完整E02/B13-B继续active，不归档，完整用途切换保持关闭。下一片为浏览器原件交付：先抽通用二进制编排并接Task/Item DTO、file-url、点云和精确camera link；再解决原生视频的受限URL凭证、完整stage复用及Range/HEAD/If-Range。不能直接给无Bearer的fetch或video src塞现有认证URI，也不能每次seek重新下载整个大文件。派生媒体、ML/Worker、导出和训练/评估/AL源继续分别接入；不将本片图片闭环视为全部原件读取完成。

## E02-B13-B2-A1 Outcome — 认证通用原件下载基础

主集成 `ef1d991ed`，来源/授权与协议分别集成于 `2a44235f7`、`95d02a94c`。六个新增身份/完整下载入口覆盖TaskOriginal、直接/primary Item、精确相机link及DatasetItem。身份只读保存事实；下载固定content UUID/SHA，沿保存位置完整核验，准入后和交付前重核原请求凭证、当前可见性、完整关系与Gate。相机按Task权限，不开放任意Item；Dataset路径与管理权限共同检查，合法相机不依赖主原件存在。

通用原始字节不经过Pillow，保留空文件、GIF/SVG、视频及点云字节；原图片解码与引用合同保持。下载512MiB、独立两槽、复制/响应各120秒合作式期限，远端transport不超过30秒。槽覆盖真实关闭；发送失败、重复取消、迟到读/关闭均drain，未知本地关闭保留额度。复核补齐退休stage在授权失败或响应构造失败时的异步关闭，避免慢关闭阻塞事件循环。无Range/HEAD/304捷径，无签名URL降级，也未替换现有浏览器消费者。

最终297个不同本地用例全部通过，无剩余失败或跳过：114项API/服务/数据库（15下载HTTP、24编排、44来源、20既有原件HTTP、11图片HTTP），44项新二进制协议/关闭纯测，135项既有reader/图片适配/快照纯测，4项SDK合同。关闭helper变更后复验5个HTTP失败路径和24项编排，不重复累计。SDK首轮因新originals标签尚未加入watched_tags而失败，补齐分类后通过；未宣称SDK已提供下载方法。

真实TCP/MinIO八组通过：签名暂存PUT并封存独立视频Task；当前用途桶放置同键错误对象后，主点云、精确相机SVG、DatasetItem及TaskOriginal完整读取仍正确；Range416/HEAD405、无凭证401/陌生人404及命中ETag的同大小损坏409。首轮探针把/minio代理地址交给没有代理的临时Uvicorn导致PUT404，改用探针专属实际MinIO公开地址后通过；共享配置未改。临时API已关闭。本片无迁移、UI或Worker逻辑变更，不重复视觉矩阵、视频播放或销毁源恢复资格化。

API/用户/架构文档、README、CHANGELOG、OpenAPI与路由索引同步；导出/一致性、Web codegen/typecheck、文档构建、Ruff/format、正常hooks和diff检查通过。人工API自动重载已加载新增入口，3100/8100、两个各72任务Worker及原队列、Beat保持；24对象、12源文件和11业务指纹未变。一次性test命名空间的数据库、Redis和九桶已独立查验不存在；两棵干净且补丁等价的隔离树/分支移除，测试和构建产物清理，人工环境、本地虚拟环境与生成类型保留。

完整E02/B13-B及整套epic继续执行，完整用途切换关闭。下一片B2-A2接通Task/Item DTO、旧file-url、工作台图片、点云/相机及邻帧预取，按账号/会话/content身份管理鉴权请求、缓存、Blob地址和取消，并限制相机并行读取；原生视频另按完整stage复用和受限能力凭据实现Range/seek，派生物与其他原件消费者仍分别收口。

## E02-B13-B2-A2 Design — 浏览器认证原件与会话资产

基于 `94fc30001` 的三路只读核对，本片关闭非视频Task/Item DTO、旧file-url及点云/相机manifest的当前桶原件签名，并完成对应浏览器读取链。视频DTO、原生播放/代理及精确帧派生合同继续由B2-B处理；缩略图/金字塔是独立派生物，不能冒用原件SHA。未知legacy条目仍应出现在列表，不能把单条严格解析409变成整页失败。

Task/Item DTO新增可空 `original_identity_path`，只表示可请求的认证身份入口，不声明已核验或可读取，不做列表N+1查询。点云manifest同样给出Task和精确camera link的身份路径及真实link UUID，原件URL为空；保留标定、排序、scene/pose和合法相机元数据。前端只为激活帧/所需预取解析这些身份，再以content UUID/SHA组成的受控下载路径作为资源版本；不得按稳定Task路径永久缓存旧内容。原件未知按当前资源显示错误，不回退Task.file_path或旧签名地址；旧file-url在当前可见性检查后明确409引导认证入口。

```text
Task/Item list -> lazy authenticated identity path
Active image/frame -> fixed resource identity -> owner/content/SHA download path
                 -> shared two-lane download admission -> Blob/ArrayBuffer
                 -> session-owned image/bitmap/cache leases -> renderer / download
```

公共读取模块只接受Task/Item/camera UUID资源或经严格语法核对的A1路径，内部构造同源API请求；核对服务器返回的download_path，不向任意URL附加Bearer。共享两槽只覆盖完整二进制响应，前景优先于预取，排队和请求可取消，仅对明确capacity429有限重试。owner为账号与不透明session，内容键另含owner UUID/SHA；同session令牌刷新不冒充新资源，旧请求结果不能注销新凭证或写入退休owner。跨标签凭据不完整时停止读取。

托管图片先由既有resolver决定是否真正使用single分支，再读取原件；required/ready pyramid不偷偷下载完整原件。显示适配器按真实字节识别PNG/JPEG/WebP/BMP/GIF/TIFF/SVG，保持原始字节；SVG使用opaque data URL，其他位图用正确MIME的Blob地址。解码失败与服务器拒绝分开显示；刷新/换Task/关闭/登出完整撤销地址。source identity包含地址版本，避免ImageStage保留已revoke旧地址。Review/DataManager显示读取状态和重试，读取失败禁止依赖该图的审核/写入；Dataset检查器认证下载在退出owner后不得迟到触发。

点云五类缓存及保留manifest按账号/session隔离，退休时取消请求与解码、清除旧几何/深度/Blob并释放无引用Bitmap；使用中的Bitmap待渲染器释放才close。底层共享下载由独立controller拥有，各消费者取消不杀其他持有者；前景可加入已开始的预取。相机面板/放大、CPU采样、GPU贴图和FramePicker邻帧缩略图统一使用同一认证资源，不把需Bearer的URL直接赋给img。每次await后重核owner与取消；refcount覆盖预取到深度计算完成，避免LRU提前关闭Bitmap。

超过八文件，分离后端DTO/manifest、点云/相机、图片三棵隔离树；主树拥有公共读取/会话边界、集成与实机验收。验证覆盖未知历史整页成功、零原件签名、身份失败无降级、原图片/reference/pyramid回归、两槽与排队取消、共享消费者退出、账号/同账号新session/令牌刷新、迟到fetch/decode/error、所有地址/Bitmap释放及Canvas读取。使用独立一次性环境实际验证图片、点云和六相机/邻帧，明暗及桌面/窄屏、控制台与请求失败；保留3100/8100和原业务/原件，清理测试产物。无新增依赖、配置、迁移或服务；回退不删除原件责任，完整用途切换继续关闭。

## E02-B13-B2-A2 Outcome — 浏览器认证原件与会话资产

公共传输 `0100d36ca`、断连/凭证保护 `a79c35daf`，点云与图片分别集成于 `1f46a121d`、`3071f4506`，后端DTO `791ce8f30`，最终集成 `e66fc5b5b`。非视频Task/Item只返回惰性identity入口，不为列表逐条查归属；未知legacy仍能列出。点云manifest保留标定、scene/pose和精确camera link，不再签原件；主原件缺失不吞合法相机元数据。旧file-url先检查当前可见性，再明确409并在OpenAPI标记弃用；视频及派生物另片处理。

公共读取只为严格Task/Item/camera UUID和同源受控下载路径附加Bearer，以账号、不透明session和content UUID/SHA隔离。两槽覆盖响应正文，前景可提升预取，排队/读取可取消，只对明确容量429有限重试。同session令牌更新保持owner，迟到401/错误不注销新会话。服务器在响应建立前也监听真实断连，重复取消仍等候物理线程和迟到stage关闭，不提前释放槽。裸FastAPI及两层BaseHTTPMiddleware的真实TCP断连探针均验证收尾。

Workbench、Review、DataManager及Dataset检查器接认证原件。先决定single/pyramid，required/ready pyramid不偷偷读取完整原件；真实字节适配SVG和位图，地址版本/显式来源绑定防止复用已撤销地址。错误保留明确重试和写入保护。连续创建意图在图片等待/失败期间保持，实际创建仍锁定；项目、工具绑定和owner改变才清除，避免切题后悄悄退出连续创建。检查器退出或账号改变后不迟到触发下载。

点云完整字节、解码帧、相机Blob/Bitmap及深度缓存按owner管理，共享下载有独立controller和消费者租约。相机面板、放大、CPU/GPU采样、邻帧和标定共用认证身份；每次await后复核owner，使用中的Bitmap最后释放才close。退休时销毁几何、GPU资源及射线命中引用，取消旧相机视角保存回调；正常切帧仍保持原子替换。实际Chrome发现SVG Blob不能直接createImageBitmap，改为opaque Image解码后创建Bitmap，取消及迟到解码仍完整释放。

主树最终587个不同Web用例、133个API/服务/夹具用例及4个SDK合同用例通过；同名复验不重复计数。覆盖原件请求、缓存/队列、跨owner/令牌刷新、401、地址/Bitmap释放、连续创建、标定和旧fixture合同。旧DTO测试刷新数据库生成时间，两个RLE种子在业务事务前独立提交既有静态绑定，fixture保存已知原件的SHA/大小；没有放宽生产读取校验。OpenAPI、本地生成类型、完整Web类型检查、CSS token、文档构建、Ruff/format、正常hooks及完整范围diff检查通过。

一次性18100/13100实际浏览器最终31个不同用例通过：9个图片/审核、21个严格WebGPU点云编辑/播放和1个DataManager只读预览。首轮连续创建回归修复后复验；离线用例受并行HMR影响，在源码冻结后通过。旧Worker总量断言改成实际postMessage角色核对：两个PCD解码、两个深度计算、零CPU上色及零提前terminate，三视图不再额外建池；GPU进程真实崩溃后的WebGL回退通过。当前Chromium适配器为SwiftShader fallback，此结果不构成RTX 3090硬件性能资格；截图单独使用软件WebGL。

命名浏览器额外检查六个独立camera link、两帧PCD、面板放大和切帧；六路SVG Bitmap均读回预期RGBA `[37,49,58,255]`，Canvas未污染。Workbench图片和检查器明暗桌面、检查器与点云明暗390宽截图人工检查通过，无横向溢出；窄屏工作台保持既有桌面提示。检查器实际下载318字节，与身份SHA相同。同session刷新保持缓存，退出后建立新session时五类缓存清零并重新加载。记录中的39个原件请求均携带同源Bearer，无向外部发送；无未捕获或console错误。两条502是隔离fixture中未运行的ML setup/capabilities，导航取消请求另计，没有原件HTTP失败。

人工3100/8100、两个各72任务Worker和独立Beat保持健康，迁移头0231。11张业务表指纹、24对象完整SHA和12源文件保持，新owner表仍为空；本片无Worker逻辑、迁移、依赖、配置、镜像或版本变化。test/e2e均停止并销毁，两个数据库、专用Redis及合计18桶独立查验不存在；命名浏览器关闭，三棵干净且补丁等价的隔离树/分支移除，探针、日志、截图、下载和测试/构建缓存按归属清理。保留人工环境、本地虚拟环境、工作树身份及生成类型。

完整E02/B13和整套epic继续执行，完整用途切换关闭。下一片B13-B2-B为原生视频：当前原件完整核验后受限能力凭据交付、stage复用及Range/HEAD/If-Range/seek，不能每次seek重新读取整个远端文件，也不能让原生video标签直接访问Bearer接口。转码/精确帧派生物拥有独立事实，ML/Worker、金字塔、导出和训练/评估/AL原件消费者继续分别收口。

## E02-B13-B2-B1 Design — 视频原件准备与可复用范围读取

基于干净HEAD `c21ca7219` 的两路只读盘点，当前原生video直接使用签名URL，平台没有Range代理；视频DTO、manifest、manifest-v2及chunk fallback仍按当前用途桶/前缀签名，Worker转码和抽帧也有独立缺口。本片先交付可独立使用的托管视频准备/读取API及实际原生播放资格，不更改这些尚未适配的既有调用者，不把协议基础称作视频迁移完成。转码、poster、frame、chunk不能沿用原件SHA；后续浏览器生命周期与派生归属分别接入。

最小方案是每次Range重新完整捕获，远端大视频反复复制不可接受；选择有界、进程内匿名stage registry。复用 `original_binary` 的冻结来源/原Authority/当前权限和Gate合同，抽出明确的授权快照与重核入口，既有完整下载调用保持行为。Task与DatasetItem的Bearer POST `original-playback` 提交content UUID、SHA和客户端session UUID；仅接受当前托管video，未知历史、reference和非video明确拒绝。session只划分客户端资源命名空间，不是服务端授权证明。完整stage仅在初始、准入后和交付前重核成功后发布，保存原Authority和完整OriginalSource，不保存原JWT/API Key。

```text
Bearer prepare -> frozen authority/source -> bounded registry reservation
               -> durable Location admission -> strict full capture -> publish lease
Native GET/HEAD + scoped cookie -> pin -> current authority/source/Gate recheck
                               -> independent-offset range response -> drain/unpin
Release / expiry / shutdown -> retire -> drain captures and response pins -> real close
```

选择无秘密的 `/api/v1/media/original-playback/{lease_uuid}` 地址和随机256位能力Cookie，不把能力或主凭证放query/path。Cookie为host-only、HttpOnly、SameSite=Strict，Path限定准确租约，HTTPS时Secure，最大300秒且不超过原Authority自然期限；仅GET/HEAD使用。Bearer DELETE按当前账号及准确Task/Item、session与lease解除本人租约并清Cookie。当前User/Key、JWT blacklist/generation/自然期限、Task可见性或Dataset管理权限、完整来源/Location/Gate必须在每次GET/HEAD之前重新检查，包含HEAD与416；旧Cookie不能绕过撤权。Origin存在时只接受同源，响应CORP same-origin、no-store/nosniff；不开放任意URL、CORS能力或共享宿主目录。

registry按Authority、session、资源与完整冻结来源复用ready stage；同键尚在准备时明确429和短Retry-After，调用者可重试，不建立复杂的共享在途取消协议。单原件5GiB、两个独立stage槽，因此最多10GiB声明暂存；槽覆盖准备、保留、活动响应及真实关闭，未知收尾继续占额。准备900秒合作式期限，远端transport30秒；每个响应120秒及256KiB分块，最多8个同时响应。租约固定300秒，stage最迟3600秒退休，每stage最多32个有效租约；重复准备可复用未退休stage并取得新租约，旧租约须显式释放或到期。30秒reaper与应用lifespan收尾保证无人访问也回收；先退休禁止新pin，待实际读线程/pin结束再关闭，关闭未知不释放额度。没有LRU强制驱逐正在使用的stage，也不默默突破预算。

采用强SHA ETag、准确长度和单范围200/206：闭区间、开区间、后缀和越界416；HEAD忽略Range且无正文。If-Range只接受精确强ETag，日期/弱/不同值回完整200；未知range unit及多范围忽略并回完整200，畸形bytes语法400，越界带 `Content-Range: bytes */size`。每次先授权再给状态/长度。范围通过os.pread独立偏移，不共享seek/read游标；响应取消或发送失败先drain已开始线程再unpin。规范依据[RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#name-range-requests)。核对了[Starlette FileResponse](https://github.com/Kludex/starlette/blob/main/starlette/responses.py)的范围/关闭生命周期和[Django FileResponse](https://github.com/django/django/blob/main/django/http/response.py)的明确资源closer；现有匿名文件、全局额度和共享pin不能直接由普通FileResponse承担，故保留小范围响应适配器。[Cookie合同](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie)用于原生同源媒体；不添加日志脱敏依赖来迁就URL能力凭据。

脆弱假设是当前API部署单Uvicorn进程。进程内stage只资格化单实例；重启、reload或请求落到没有该lease的实例返回410，GET绝不隐式重拉整个远端原件。后续客户端收到410须Bearer重新prepare；多副本共享暂存/协调仍需独立设计，不能用进程内10GiB声称集群总预算。当前/tmp可用约94GiB，工具与本机MinIO已验证；预算为本片明确常量，不新增环境变量、服务、外部凭据、依赖、迁移或镜像。撤销新增API即可回退，不修改原件归属或已有派生数据。

预计超过八文件和三个协作组件：隔离树分别实现stage/租约生命周期与纯测试、单范围响应与真实取消测试；主树拥有授权快照、路由/schema/lifespan、真实DB/API/MinIO和原生浏览器验收、正式API/架构/运维文档、README/CHANGELOG与生成合同。验证保存桶陷阱、未知/非video零I/O、并发prepare容量、重复seek零额外S3复制、独立偏移并发、HEAD/If-Range/空/末端/畸形与超长Range、Cookie跨lease/资源拒绝、过期/撤权/删链/Gate改变、重复取消/关闭未知/TTL/shutdown。仅用验证过一次性namespace，真实TCP和浏览器loadedmetadata/seeked/Canvas读回；生产UI未改，不重复UI视觉矩阵。人工3100/8100、两Worker/Beat和既有业务/24对象/12源文件保持，提交正常hooks及diff检查后清理精确测试资源。完整E02、B13-B与用途切换继续未完成。

## E02-B13-B2-B1 Outcome — 视频原件准备与可复用范围读取

已完成协议基础，主集成 `e7a085b41`；范围/租约组件分别为 `1df0bf575`、`a67301eb0`，边界修正为 `5f995e94b`、`fd34bb61d`。Task/Item 的 Bearer prepare、准确路径 HttpOnly Cookie、当前授权复核的 GET/HEAD、本人资源/session DELETE、进程内有界完整stage及reaper/lifespan收尾已接通。原完整下载复用授权快照并保持原行为；视频DTO、工作台与转码/精确帧消费者尚未迁移。API/OpenAPI/Web类型、SDK内部接口分类、README/CHANGELOG、架构与运维文档及ADR0107同步。

主树242个不同API/服务/纯测试及4个SDK合同用例通过；主体241项、Cookie补充2项去重计242，不重复计入组件验证。覆盖真实保存桶、错身份/非video零读取、内容损坏、JWT/API Key自然期限及撤权、关系/Gate变更、await之后租约过期/退休、同键复用/容量、独立偏移、Range/If-Range/HEAD/空内容、重复取消、发送失败、未知关闭占额及shutdown。集成发现的await后能力失效、空内容正后缀范围及If-Range求值顺序已修正；HTTPS浏览器经现有Host改写代理时仍设置Secure Cookie。首次原下载回归因测试启动目录错误未能准备数据库，改用 `apps/api` 下的隔离runner后23项通过，未放宽生产合同。Ruff/format、正常提交hooks、Web类型/codegen、最终文档构建和diff检查通过。

实际18100/13100使用归属命名空间上传并封存192191字节WebM，身份SHA与原字节相同。HEAD、两个并发独立范围、后缀、If-Range及416检查通过；暂存准备后临时移除准确源对象，范围仍正确，随后按原字节恢复，证明ready读取不再从S3复制。命名Chrome150原生video读取6秒/320×240元数据，seek至3秒后继续播放；Canvas读回 `[65,7,0,255]`，未污染，Cookie不出现在document.cookie。退出登录后旧租约由200变403；只重启一次性API后旧地址410，重新准备恢复元数据/seek/播放和相同像素。没有未捕获浏览器错误；403/410为主动验收情形。本片未资格化实际长时间TTL或多副本部署，TTL由可控时钟测试覆盖。

真实TCP在POST响应前断开，确认准确API进程/工作目录/模式及storage operation；匿名临时FD从1经对照准备的2回到1，操作账本归零，后续prepare/HEAD/DELETE成功。该探针不声称物理复制必已开始，中途阻塞线程/重复取消由纯测试独立覆盖。此次实际浏览器为协议探针，不代表工作台已接入或GPU性能验收。

人工3100/8100、两Worker及独立Beat健康，实际API已服务新增合同，迁移头0231；11张业务表指纹、24对象完整SHA与12源文件保持。本片无Worker代码、迁移、依赖、配置、镜像或版本变化。test/e2e停止并销毁，两个数据库、两个专用Redis与18桶独立查验不存在；命名浏览器关闭，两棵干净且补丁等价的隔离树/分支移除，任务探针/日志/截图/测试构建缓存按归属清理。人工环境、本地虚拟环境、生成类型和工作树身份保留。

完整E02、B13-B与整套epic继续执行，完整用途切换关闭。下一片B13-B2-B2接原生视频客户端的prepare/释放/失效恢复、Task与Item惰性身份及session/task切换；派生poster/proxy/frame/chunk必须有独立事实，不借原件SHA获得归属。其他原件消费者继续逐项实施。

## E02-B13-B2-B2 Design — 浏览器原视频与租约生命周期

基于 `cd8349a80` 的两路只读盘点，原生媒体只有VideoKonvaStage中的video；Task/Item DTO、旧manifest、manifest-v2及chunk fallback仍按当前用途桶签整段视频。精确帧只使用v2的Item/chunk信息，没有消费整段fallback地址。三个视频seed已实际写原字节但没有登记现有原件归属。新协议可处理独立TaskOriginal视频，但既有工作台metadata仅来自直接DatasetItem；本片不暗中新增独立视频探测能力。

本片统一托管video DTO的惰性身份路径，清空原件file_url；主manifest返回metadata及准确Task身份路径，video_url可空，既有playback_path不作为原件来源。v2与chunk fallback同样撤除整段视频签名。poster/frame/chunk继续明确为尚未接管的派生链路，不套原件身份。浏览器只把prepare得到的严格同源相对地址交给video；原容器/编码不能解码时明确失败，绝不退回旧file_url、file-url、转码路径或签名原件。Dataset视频的旧新页链接同步改为显式受限的认证原件下载，512MiB完整下载上限保持，5GiB仅是播放准备上限。

客户端复用OriginalOwner及身份查询，严格核对video、content UUID/SHA、准确lease/media path及有效期限；相机资源拒绝。prepare采用当前Bearer和session，完成后重核owner/取消/同session token轮换，迟到租约按准确资源释放。清理DELETE使用隔离的同源请求及捕获凭证，不触发apiClient的401全局退出，防止旧账号清理退出新账号；失败由服务端TTL兜底。HEAD仅对已校验路径用Cookie、不带Bearer，410才允许有界重新准备；403/409、编码错误与容量失败保持可辨认，不循环重试旧地址。429仅对已知准备/容量代码作有限退避。

逻辑工作台按账号/session/Task/内容身份退休，覆盖precise/preview/bitmap和迟到seek；同内容租约更新不重挂整个逻辑舞台，不丢当前帧、草稿或导航。原生传输源先pause/remove src/load，再释放旧租约；新源通过现有loadedmetadata/FrameClock对齐最新帧。到期前提前prepare并切换新路径以复用未退休stage；短自然期限不形成忙循环，失败提供明确重试。账号/任务切换、跨标签页不完整凭证、卸载/HMR均停止旧媒体和定时器，不把旧结果应用到新owner。准备和失效期间不允许仅凭metadata成功进入依赖画面的QC流程；真实源就绪参与门禁。

隔离树分别负责后端DTO/manifest/视频seed及协议客户端与纯回归；主树负责React生命周期、原生播放器/检查器与QC接入、正式文档和实际浏览器验收。验证身份/prepare失败零签名降级、迟到prepare/双取消、StrictMode、过期/410重建、codec错误不重建、撤权、同session token刷新与新session退休、切Task/关闭、暂停草稿及播放/seek/Canvas。只在已验证的一次性test/e2e环境写入；补真实视频seed归属，不放宽生产读取。保留人工3100/8100及原业务/24对象/12文件，正常hooks提交后清理精确资源；无依赖、迁移、配置、镜像、Worker或版本改动。派生归属和其余原件消费者仍属后续切片，完整E02与用途切换未完成。

## E02-B13-B2-B2 Outcome — 浏览器原视频与租约生命周期

已提交协议客户端 `83e3882e6`、视频DTO/manifest/seed `fcadc8b6d`、会话生命周期 `6c7b41e0d`、旧凭证proof退休 `bbd43dcb1`、自有夹具租约清理 `b6a9ecb69` 及工作台集成 `02c94529a`。发布里程碑未指定，不改版本。本片仅交付浏览器原视频；完整E02/B13及整套epic继续执行，完整用途切换关闭。

托管video的Task/Item只提供惰性原件身份路径，未知历史列取不触发物理读取；主manifest提供metadata和准确Task身份路径，整段video_url、v2 video_url与chunk fallback_video_url均为空。旧playback_path不参与原件选择，三个视频seed按实际原字节登记原件事实。数据集检查器用认证完整下载，512MiB下载上限保持。独立TaskOriginal已有prepare协议，但既有工作台metadata仍只来自直接DatasetItem；未新增独立Task视频探测，也不承诺浏览器支持所有原容器/编码。

Workbench先核验身份再prepare，只把严格同源相对media_path交给原生video；Cookie受准确租约路径限制，不向native请求传Bearer。账号/session/Task/内容身份管理逻辑舞台，URL轮换仅退休传输；当前帧、两点polygon草稿、时间轴窗口及视角不因同内容续租重置。替换前停止并拆除旧源，再释放lease；提前续租复用ready stage，失效以准确Cookie HEAD判别，有界处理410，权限/内容/容量/编码错误不退回签名原件或旧转码路径。JPEG编码前后核对源代次、实际像素帧和逻辑帧，恢复seek期间不交付首帧冒充的截图；严格QC保留独立像素呈现回执，不能用普通播放对齐或metadata代替。

复核明确区分两个生命周期：已挂载的同owner/task舞台遇到metadata空窗时保留已确认信息和局部草稿；登录令牌刷新仍遵循现有RequireProjectAccess重新校验并卸载工作台，旧媒体、lease与局部草稿退休，通过后重新挂载。未放宽该权限守卫，也不宣称完整应用的令牌刷新会保留未持久化草稿。组件单测覆盖挂载内空窗、Task/owner退休；实际浏览器另证权限复核期间无旧video、DELETE204及旧HEAD410，通过后取得新路径。

最终216个不同API/服务/DB用例、245个不同Web用例及4个SDK OpenAPI合同用例通过。后端覆盖DTO、workbench metadata/frames、视频seed、registry及namespace清理；Web覆盖协议验证、StrictMode、迟到prepare/取消/释放、同session token更新、前台到期恢复、暂停意图、QC、原生/精确帧、Canvas及迟到JPEG。正常提交钩子的完整Web ESLint、CSS tokens、TypeScript build检查，以及OpenAPI/codegen、文档构建与diff检查通过。统计按最终同名用例去重，不叠加中间重跑或早期全Web结果。

18个不同实际浏览器用例通过：真实同源Cookie及原生请求不带Bearer检查、原字节Canvas/JPEG、SPA离开释放、同内容续租保持F17及两点草稿、实际服务端退休后HEAD410恢复并保留F23、身份409零prepare/零native回退，以及原有指针时间轴和WebCodecs精确帧/稳定回退矩阵。身份409使用测试路由故障注入；410基于真实DELETE后的原生error事件注入，防止已缓冲媒体不发I/O掩盖恢复分支。另用独立agent-browser验收原生播放/暂停、F26错误后重试定位、明暗桌面及390px窄屏截图，窄屏保留既有桌面操作提示，无横向溢出或页面异常；控制台仅有既有Konva层数提示。未授予RTX硬件性能或大文件规模资格。

连续套件暴露的清理缺口已修复：浏览器context关闭不会执行React卸载，旧stage会短暂耗尽两槽。现有test seed cleanup在精确namespace和已有preflight之后，只撤销匹配Task/Item上下文的lease；邻居和未知上下文保持，真实active pin继续drain后释放物理文件及额度。没有加大生产容量、缩短TTL或增加宽松生产读取。错误清理未知责任仍由完整、已验证的一次性mode销毁收尾。

正式文档已更新 `docs-site/user-guide/workbench/video-playback.md`、`docs-site/api/guides/verified-content.md`、`docs-site/dev/concepts/storage-connections.md`、`README.md` 与 `CHANGELOG.md` Unreleased。没有依赖、环境配置、迁移、镜像或Worker任务改动。人工3100/8100与两Worker/Beat保留，迁移头0231；11表完整行指纹、24对象完整SHA及12源文件SHA在验收与清理后均保持。一次性test/e2e的数据库、Redis、18桶已销毁并独立检查不存在；自有浏览器关闭，两棵干净且patch等价隔离树和三分支移除，任务日志/截图/测试缓存清理。其余派生媒体、ML/Worker、金字塔、导出及训练/评估/AL原件消费者继续逐项收口。

## E02-B13-B3-A Design — 样本评估当前来源原件读取

基于 `02c94529a` 的只读调用链盘点，下一片限定现有样本评估“核对当前标注任务”入口：SampleEvaluationGallery → sampleEvaluationsApi.source → GET `/projects/{project}/sample-evaluations/{evaluation}/attempts/{attempt}/samples/{ordinal}/source` → sample_evaluation_sources.resolve_source。当前current_snapshot根据有无dataset_item_id猜选现行用途桶，read_current_media自行HEAD/GET并计算SHA，未使用保存的原件归属。本片不依赖外部Agent/S3迁移现场或视频派生owner。

只替换此API的当前原件读取：通过original_sources.resolve_task固定TaskOriginal或DatasetAsset的关系、位置、内容UUID/SHA及大小，未知归属和非image零对象I/O，保持现有无法核对语义。经现有storage_access许可和managed_original_reader完整核验，按当前已登记SHA读取，再与冻结样本SHA比较；合法新内容应报告changed，不能把冻结SHA误当当前读取预期，也不能按file_path、现行桶或同名键回退。保留评估既有16MiB、1600万像素、单帧、格式与EXIF逻辑尺寸限制，不扩大为通用下载或B1图片解码预算。

捕获准确原请求凭证，许可取得后、完整读取/解码后及标签分类返回前复核凭证、项目管理权限、Task/原件关系和Gate；保持现有datasets:read与annotations:read要求，不因复用通用Task下载增加tasks:read。撤权/过期不能被宽泛except变成普通unavailable。取消、超时和重复取消等待真实线程、Body/临时文件收尾，未知关闭保持已有许可/额度责任，不提前声明物理完成。冻结样本、label分类、source/deleted/changed等既有业务含义保持，必要错误映射按现有响应合同明确区分。

共享同步read_current_media仍被active_learning_pool使用；本片不修改其AL读取协议，不把新入口静默推广到Worker。冻结image/report、训练版本构建、AL pool/预标注、模型推理、金字塔、导出、视频派生物及完整用途切换均排除，另片处理。预计改动限来源服务、现有source路由与专用回归，必要时新增窄读取适配；复用现有owner、reader和权限helper，不新增原件owner或第二套S3读取器。

验证保存桶陷阱、直接DatasetItem/独立TaskOriginal、未知历史零I/O、当前合法内容变化与冻结比较、关系/Gate/权限/JWT或Key在读取与标签分类期间改变、重复取消和未知关闭；测试对象必须按(bucket,key)区分，不能用现有仅key替身证明固定位置。复用test_sample_evaluations与test_sample_evaluation_masks回归，使用已验证的新一次性test/e2e环境做真实HTTP/MinIO及既有核对弹窗验收。人工3100/8100及原业务/原件保持；正式API/用户文档与CHANGELOG随实施更新，正常hooks提交并清理精确测试资源。本设计待实施，不宣称B3或完整B13完成。

## E02-B13-B3-A Outcome — 样本评估当前来源原件读取

主集成 `6b094074c`，显式准入责任修复 `fe9961828`，生命周期回归 `7ffaa7d52`。仅替换现有“核对当前标注任务”source API 的当前原件读取；完整B3、B13、E02和整套epic继续执行，完整用途切换关闭，不分配发布版本。

新适配沿 `original_sources.resolve_task` 固定当前DatasetAsset或已发布TaskOriginal及保存位置，取得同一Gate修订的耐久许可后完整核验当前已登记SHA/大小，再与冻结评估SHA比较。保存桶不会被现行桶同名对象替代；合法重新登记的新原件报告changed，同键覆盖、未知归属、缺失或解码失败不产生成功SHA。保留16MiB、1600万像素、单帧、既有Pillow格式、EXIF逻辑尺寸和标签分类语义。共享同步read_current_media保持，未静默迁移主动学习。

授权保持项目管理权限及datasets:read、annotations:read，不增加tasks:read；准确JWT/Key、账号、项目、完整源关系和维护Gate在读取、分类与真实关闭后复核。任务/标注的最终快照移至物理关闭后，防止慢关闭期间的变化生成成功回执。JSON操作复用断连监听但自行拥有清理，取消/超时及重复取消等真实线程退出。显式两槽在未知constructor、Body、client或本地close时保留责任；已存在的unsettled异常即使被关闭阶段取消覆盖，也继续传递到Permit，不能误清账本。正常已关闭临时文件的额度可释放，不因此抹去其他未知责任。

最终350个不同后端API/服务/DB及纯测试、4个SDK OpenAPI合同和12个页面回归通过。覆盖精确(bucket,key)替身、保存桶陷阱、独立TaskOriginal、未知/非图片/超限零原件I/O、篡改、合法新登记、读取/分类/关闭三个边界的JWT/Key/账号/Gate/原件/标注变化、真实Pillow格式与EXIF、线程/FD收尾及未知责任。初轮新夹具试图修改verified内容事实被数据库不可变约束拒绝；改为合法插入新原件/重绑Task，没有放宽生产约束。只重跑受修复影响的用例及显式准入的现有视频调用者，统计按最终用例去重。

真实HTTP/MinIO十项检查覆盖保存桶同键陷阱、原有两个scope、独立TaskOriginal、同键覆盖拒绝与恢复、合法新原件、标签变化、未知归属、任务删除及冻结报告保持。现有浏览器弹窗实测相同/无法核对/已变化/已删除，Escape归还焦点；实际打开annotate并返回保留attempt/sample，删除后不提供其他Task跳转，无未捕获页面异常。源对象和报告读取均使用真实字节；候选、训练和推理记录为隔离合成夹具，后台setup不可达的提示不属于模型资格证明，不宣称真实训练、推理或GPU性能验收。删除探针初次被Annotation外键正确阻止，先清除其自有标注再删除自有Task后完成；没有绕过外键或触及人工数据。本片无UI样式变化，不重复视觉矩阵。

README、CHANGELOG Unreleased、评估用户指南、API合同和存储架构文档已同步；OpenAPI导出无合同漂移，本地codegen、Web类型检查、docs build、Ruff/format、正常提交hooks和diff检查通过。没有依赖、配置、迁移、镜像、Worker任务或版本变化；Worker旧调用的共享同步读取与分类默认行为未变，不需刷新消费者。

人工3100/8100、两Worker和独立Beat保持健康，迁移头0231；11表完整行指纹、24对象完整SHA和12源文件SHA不变。test/e2e的操作账本在销毁前均为零，两个数据库、两个Redis和18桶已销毁并独立查验不存在。自有浏览器关闭，两棵干净且patch等价的隔离树/分支移除；任务报告、探针、截图与构建/测试缓存按归属清理，保留人工环境、本地虚拟环境、生成类型及工作树身份。

## 下一片范围 — 主动学习原件输入

只读盘点已确认共享同步read_current_media仍被active_learning_pool.read_input使用，实际物理调用包括pool.prepare的预览/创建、active_learning.prepare_image的后台PNG补建，以及decide的接纳前原图复核。只替换预览会遗漏后两条路径，不能宣称主动学习已迁移。

下一片先统一这三条调用的冻结内容、保存位置、当前权限及原凭证边界，再定实施切片。保留32成员/128MiB总量及检测/分割独立解码预算、候选去重、历史排除、preview_digest/冻结spec与既有报告兼容性；旧spec不能按现行桶或文件名补造原件归属。后台补建和接纳须以冻结内容核对当前真实归属，已有合格PNG仍沿自身输出owner，不把派生SHA当原件证明。明确API断连与Worker取消的真实收尾责任，不能将await asyncio.to_thread直接视为物理取消。完成调用链和兼容策略定案后实施，不在本片修改AL协议；冻结评估媒体、训练版本构建、其他ML/Worker、金字塔、导出及视频派生链继续各自推进。

## E02-B13-B3-B Design — 主动学习三处原件输入

基线 `2b3d9c541` 的两路只读盘点确认：pool.prepare负责预览/创建，prepare_image在Worker缺失冻结PNG时回源，decide在接纳前复核完整去重/Scene组。三处共同迁移至窄AL原件适配，删除共享猜桶raw读取，不能只修预览。新snapshot.original固定修订1、owner_kind、内容UUID/SHA/大小和逻辑location_id；不暴露桶、端点或凭据。每次沿canonical owner的保存位置解析完整OriginalSource，读取许可后、归一化后及真实关闭后全量比较。维护Gate代次只冻结于本次操作，不把合法stop/resume或受控恢复桶映射永久固化进跨轮次规格；同SHA换owner/逻辑位置仍拒绝。

新规格声明original_contract=1，全部pool成员须有一致原件事实。旧规格、报告和已有合格PNG保持，缺少原件事实的历史轮次不能回源补图或新增接纳；不能据当前owner回填旧证据。合格派生PNG继续沿自己的输出owner，坏PNG不回源静默修复。候选分组/历史排除、preview_digest、32成员/128MiB及检测/分割预算保持。

API捕获准确JWT/Key并维持原datasets:read/write、annotations:read；创建和接纳的最终事务在既有业务锁下复核原请求、完整原件与Gate，辅助检查不得提交Job/Batch事务。新轮次authorization只保存原Key proof的不可逆指纹，后台持续核对创建者、当前权限和原Key；不持久保存Bearer或把JWT短寿命套到24小时后台窗口。Worker异步拥有准备流程，复制、归一化、PNG写回和关闭均等待真实线程收尾，发布前复核attempt/claim/generation/status及原件。未知关闭保留额度与耐久许可，取消后不能生成可用输入或迟到发布。

主树负责AL适配、三个业务入口、Worker调用、跨入口回归、文档与真实验收；隔离树升级AL自有原件夹具和相关现有用例，不修改共享key-only报告替身。使用已验证新test/e2e命名空间、真实MinIO/HTTP与当前checkout Celery；合成completed收据只验证输入执行链，不宣称真实模型推理。刷新受影响人工Worker通过原监督服务进行，不重建镜像；保留人工3100/8100、Beat、业务/24对象/12文件。正常hooks提交后核对精确资源清理，完整E02/B13和用途切换继续未完成。

## E02-B13-B3-B Outcome — 主动学习三处原件输入

夹具与既有调用回归 `7a9988ac2`、物理生命周期回归 `513282196`，主集成 `d888f5051`。预览／创建、Worker缺失PNG重建及接纳前整个去重／Scene组均沿已登记原件解析与保存位置读取，删除猜桶的共享同步读取；完整E02/B13与整套epic继续执行，完整用途切换关闭，不分配发布版本。

新规格original_contract=1固定安全原件收据：内容owner、UUID/SHA/大小及逻辑位置，不把桶、端点、凭据或后台Key指纹放入公开来源快照。每次物理操作固定完整关系、位置及Gate代次，复制／归一化和真实关闭后重核；同SHA换owner或逻辑位置仍拒绝，正常维护恢复与受控桶映射不改跨轮次身份。32成员、128MiB总量、16MiB单图、1600万像素及既有检测／分割、分组和排除预算保持。旧spec不回填，报告、已有合格PNG与拒绝仍可用，缺少原件事实的回源补图和新增接纳明确409；坏PNG不静默回源修复。

HTTP维持原三个scope并复核准确JWT／Key；新后台authorization保存原Key proof不可逆指纹，轮换同ID密钥、scope或期限改变均拒绝。最终创建和接纳在原业务事务与锁内复核，helper不提交Job／Batch。Worker异步拥有读取、PNG写回／完整读回及关闭，以attempt／claim／generation／status阻止迟到交付；真实线程未退出或关闭未知仍持有责任。实际轮询暴露Key访问记录更新与NOWAIT复核冲突：现以连接级SAVEPOINT有限重试，不flush／提交外层业务写入，真实撤权不重试；持续暂忙保持可恢复lost，不误报撤权或损坏输入。

最终322个不同本地后端API／服务／数据库及纯用例、4个SDK合同和30个Web用例通过，无剩余失败；13项既有按用途不适用的参数组合按原条件跳过。统计按最终同名用例去重，包含66项物理生命周期回归，不重复累计重跑。覆盖三条原件路径在解码／真实关闭期间的Key、账号、Gate与owner变化，读取前换owner零I/O、预览到创建的代次变化、JWT关闭后过期、旧规格兼容、真实格式／EXIF、反复取消、未知关闭、事务不提前提交及独立数据库事务的Key锁冲突／真实撤权／持续占用。

一次性18100/13100跑通12项真实HTTP／MinIO／当前checkout Celery检查：四个原件含独立TaskOriginal，保存桶的同名陷阱不替代原件；原三个scope创建、后台补图／完整排序、四张保留PNG摘要、同键篡改拒绝且零决策、恢复后完整接纳并创建人工批次通过。推理端只提供合成completed收据，实际派发计数零，不构成真实模型推理或GPU资格。浏览器确认完整排序、16像素真实候选图片、4/4接纳与固定批次历史，实际跳转批次设置，无页面异常或横向溢出；本片无UI样式变化，不重复视觉矩阵。fixture的健康观测须刷新后才通过现有资格检查，没有削弱生产检查。

README、CHANGELOG Unreleased、用户／API／架构文档同步；OpenAPI导出无合同漂移，codegen、Web类型检查、docs build、Ruff／format、正常提交hooks和diff检查通过。无依赖、配置、迁移、镜像或版本变化。人工Worker确认idle后通过原监督服务刷新，两个各72任务注册及队列保持，实际无owner UUID的选样任务派发返回；独立Beat未重启，3100/8100和迁移头0231正常，11表完整行指纹、24对象完整SHA和12源文件不变。

验收环境操作账本为零；test中的三个storage-adoption未知关闭账本来自既有故障用例并按合同保留，未伪造清零。测试客户端退出后，两个完整自有一次性mode销毁，数据库、Redis和18桶独立查验不存在。自有浏览器与合成收据服务关闭，两棵干净且patch等价隔离树／分支移除；任务报告、探针、截图与测试／构建缓存按归属清理，保留人工环境、本地虚拟环境、生成类型及工作树身份。

## 下一片范围 — 点云质检Worker原件输入

只读盘点确认点云质检仍沿create_quality_run／build_source_snapshot → point_cloud_quality Worker → execute_scan → \_read_normalized_pointcloud／\_read_pointcloud按当前datasets_bucket读取PCD，并按路径与轴约定缓存。下一片先定稿冻结原件owner／内容／逻辑位置、后台持续reviewer权限及准确任务可见性、取消／代次与真实解析收尾的合同；不能借用AL的manager权限替代既有REVIEW_WRITE，也不能为旧快照按当前owner补造证据。保持256MiB、5000 Task／20000标注、ASCII／非压缩binary PCD及ISO轴归一化；相机只读标定／尺寸，像素与派生链另片。实施前进一步复核当前调用者、历史结果兼容与发布事务，未将盘点称为该消费者已迁移。

## E02-B13-B3-C Design — 点云质检Worker原件输入与统一发布

基线 `062bb12db` 的两路只读复核确认：质检仍按当前datasets_bucket和路径读取PCD，缓存键仅路径／轴约定，异常宽泛吞为skip，逐标注／轨迹提交问题且末端无来源／取消复核。本片同时接权威原件和结果发布事务，不能只替换GET；保持256MiB、5000 Task／20000标注、ASCII／非压缩binary PCD、ISO轴归一化及相机仅标定／尺寸的边界。

新scope声明original_contract=1，各现有pointcloud输入固定安全原件owner／内容UUID／SHA／大小／逻辑位置收据；保存桶、端点与Gate代次只由本次canonical解析固定。未知／不支持输入可保留明确skip但零原件I/O，不能猜当前桶；已有Scene解析仍依赖direct／primary Item，不新增独立Task的Scene注册。缓存包含内容身份与轴约定，命中仍复核当前原件、轴、权限和Gate。读取、解析、归一化与真实关闭都由耐久访问许可和显式两槽覆盖，取消／超时等待实际线程，未知收尾保持责任。

保留REVIEW_WRITE及准确Task可见性；project／scene范围仍限manager，task／annotation范围保留reviewer。该入口目前没有API Key scope要求，不因复用AL／训练helper新增datasets或tasks scope。HTTP捕获准确原请求凭证，后台委派记录放AsyncJob隐藏payload，持续核对账号、原Key指纹与项目职责／Task可见性，不保存Bearer或依赖JWT短寿命。新singleflight包含原件合同与委派身份，后来的请求不改写旧运行授权。

Job→Run短锁原子认领pending并固定Celery任务绑定；计算阶段只保存有界finding候选、更新进度，真实原件收尾前不写Issue。末端以同取消分支的Job→Run顺序锁定，刷新授权、任务与完整来源／配置／标注快照、所有已读OriginalSource／Gate，然后统一upsert问题、完成Run／Job和通知，同一事务提交。异常先rollback，再新事务登记失败／stale／取消，保留已取消终态和既有人工复核，不能提交半轮新问题。来源锁覆盖Task、Scene、相关标注／轨迹／区间／pose、Dataset及canonical原件；保持按现有写入协议的短事务，不在远端I/O期间持有业务锁。

旧completed结果继续作为历史读取，不回填原件事实；缺少合同的旧pending／running不得回源，标为stale。新问题证据保存安全原件收据与轴，现有staleness按Task去重核对owner／逻辑位置／大小及轴变化；旧原件相关问题缺少事实时明确stale，不能混入当前治理样本，纯轨迹历史保持。无数据库迁移、依赖、配置、镜像或版本变化。

主树实现服务／Worker／API取消围栏、新合同与回归、文档及真实验收；隔离树升级现有PCD夹具和内调用，不修改生产。使用新且验证所有权的test／e2e模式做实际PCD字节、MinIO、HTTP／Celery和当前质检页面验收；保留人工3100/8100、两Worker及Beat和11表／24对象／12文件指纹。正常hooks提交、精确清理后记录Outcome；完整E02/B13与整套epic仍未完成。

## E02-B13-B3-C Outcome — 点云质检Worker原件输入与统一发布

主集成 `d0761af78`；既有PCD夹具接入 `3461b4ce8`、`5503c7f15`，物理生命周期回归 `b699c2fd3`。发布版本未指定。本片不新增迁移、依赖、配置或镜像构建，数据库保持0231。

- 新运行固定安全PCD原件收据及轴约定；canonical Task关系决定已保存位置，完整SHA／大小读取拒绝当前桶及同名替代。缓存包含owner／内容／逻辑位置／轴，命中仍重核来源和当前权限；显式空轴约定沿用ISO默认值。
- 保留原REVIEW_WRITE、manager范围和准确reviewer Task可见性，不增加API Key scope。HTTP使用准确原请求，后台隐藏保存原Key指纹并持续复核；提交前自然到期也回滚。旧pending／running无合同在派发或重放时stale且零原件I/O，历史completed保留，旧原件相关问题明确过期。
- Job→Run短锁只领取一次，读取／解析／关闭全部实际收尾后再锁定完整来源、配置与授权，统一提交Issue、Run／Job及通知。末次关闭取消、写入中断或期限失效不遗留半轮问题；已有人工判定保留。点云质检已加入终态通知白名单，并修复带轨迹的逐框问题误用空revision而立即过期的问题。
- 累计158个不同后端用例通过，包含真实ASCII／binary PCD、原件位置与owner、解析／关闭后10组迟到变化、两槽与未知收尾责任、末次关闭取消、原子写入回滚、Key自然到期、同名不同owner缓存、两个真实数据库会话并发认领，以及旧运行／旧问题兼容。既有纯轨迹staleness查询预算保持；7项质量面板／治理Web回归通过。仅统计最终通过的不同用例，不重复累计重跑。
- 一次性18100／13100的真实HTTP／MinIO／Celery完成四轮：保存桶原件生成4条问题、同合同复用、对象篡改明确skip且无伪点数问题、轴变化令旧证据stale及新轮完成；浏览器另以JWT发起当前Scene扫描并确认1条问题，回读为resolved／confirmed。浏览器标准primary_lidar关联补在合成夹具中，不修改生产Scene注册边界。明暗截图、无页面横向溢出及无捕获到的JS异常已核对；此为现有确定性PCD质检，未授予ML或GPU性能资格。
- OpenAPI快照与现行路由一致；本地Web API生成、类型检查、Ruff、格式、文档构建、正常提交hooks及`git diff --check`通过，未运行远端CI。
- 人工环境通过原监督服务刷新并恢复3100／8100，两Worker各注册72项且实际无业务输入派发成功；绑定的独立Beat随原监督服务恢复，已核对使用本工作树dev模式。11表完整行指纹、24对象及12个文件SHA均与本片前一致。自有浏览器关闭，test／e2e两个数据库、两个Redis及18桶已销毁并独立核对不存在，三棵已集成隔离树及任务产物清理。

正式资料已同步README、CHANGELOG Unreleased、`docs-site/user-guide/workbench/3d-box.md`、`docs-site/api/guides/verified-content.md`与`docs-site/dev/concepts/storage-connections.md`。完整E02／B13及整套epic仍在执行，完整用途切换关闭，不归档此活动计划。

下一片优先定稿训练数据版本的托管图片预检与Worker缺失媒体回源：`freeze_selection→capture_source_media`及`materialize_sample→materialize_image`仍冻结当前source_bucket／ETag／size，现有版本snapshot、固定输出storage_ref和attempt围栏可复用。保持引用图片、已保留媒体、历史ready包和原管理者／scope合同；金字塔及LiDAR导出还涉及独立派生output归属或额外nuScenes文件，继续分别实施。此项仅完成只读盘点，尚未定稿或实现。

## E02-B13-B3-D Design — 训练数据托管图片原件与物化

基线 `699acafa6` 的只读复核确认：托管图片预检只按当前桶冻结ETag／size，缺少保留媒体时Worker直接回源；保留命中只比可改写的对象metadata SHA，原生线程取消后仍可能访问已退出的临时目录。新片覆盖预检、创建、自动策略／种子内调用、Worker回源及ready发布，不只替换GET；引用图片继续执行独立asset_source_contract，已有输出storage_ref不换owner。

新snapshot声明managed_original_contract=1，托管sample记录安全owner／内容UUID／SHA／大小／逻辑位置收据，不再保存当前桶作原件证据；预检摘要包含该身份，单图256MiB、100MP、既有格式／EXIF、20GiB／版本及20000样本保持。先核对声明总量再实际完整捕获、解码及关闭，所有原件读取沿canonical Task direct／primary或独立TaskOriginal保存位置执行。缺失／不支持输入明确阻止ready预检；权限、绑定、Gate、取消或未知收尾错误不能被宽泛media_unavailable吞掉。

HTTP固定准确原请求证明，snapshot.authorization私有保存原Key指纹，后台与retry继续核对它；不保存Bearer或把JWT短寿命委派给Worker。保持manager及datasets:read／annotations:read，不增加datasets:write／tasks:read。自动策略和种子使用显式内部构造入口，策略原有更严格scopes仍检查，不能把request=None当HTTP已授权。

预检／创建采用显式prepare和finalize阶段：业务事务持有的项目advisory、User／Project及Policy／Trigger锁不能跨物理读取，也不能由媒体helper隐式commit。先保存本次选择与调用方上下文，短事务释放后完成所有物理工作；末端重新取得原串行化锁，复核调用上下文和选择／标签／原件事实，再原子保存Version、membership与Job。策略重取锁后复核启用状态、revision／config、基线和活动trigger；后来的策略编辑或并发检查不得覆盖。Worker沿原Version→Job取消顺序及执行advisory锁，不移植PCQ相反顺序。

物化先从版本固定输出owner读取保留媒体，新合同同时核对冻结原件SHA／size，拒绝重写metadata伪造；仅保留缺失时读取固定原件，完整暂存、复制、解码、输出PUT／回读和真实关闭之后再复核当前权限／来源／Gate。本次回源的完整OriginalSource只在内存保存，ready末端复核；保留媒体命中不要求已删Task／源位置仍可用。训练标签在创建后已冻结，ready不重跑当前标签选择。旧ready包和合格历史保留媒体仍可用，旧无原件事实且保留媒体缺失时零raw I/O拒绝回源，不回填当前owner。

原件以独立两槽及耐久访问许可覆盖物化到真实关闭，未知关闭保留责任；输出媒体／mask、ZIP构造、包上传／校验与失败清理线程均等待实际收尾，才能退出TemporaryDirectory和释放执行锁。包ready仍要求当前版本／attempt／Job／owner、创建者权限与原Key、所有本次回源事实有效，最终自然期限复核后统一提交状态及通知；取消或迟到变化不发布ready。保留媒体和包的实际写回／回读沿固定output owner，不授予完整用途切换或物理回收许可。

验证使用本工作树新test／e2e模式、实际图片／RLE及独立HTTP／MinIO／Celery；保留3100／8100、两Worker／绑定Beat和11表／24对象／12文件指纹。不增加依赖、环境配置、迁移、镜像或版本；实施与验收见下方Outcome，完整E02／B13与整套epic继续执行。

## E02-B13-B3-D Outcome — 训练数据托管图片原件与物化

主集成 `9b2f1a946`；调用方阶段拆分 `3f90f0c4c`，种子身份登记及首次分割初始化修复 `93702cf27`、`d0e52d1ce`，终态信号锁序 `9ee3e7393`，正式文档 `fac575b7e`。生命周期与Worker边界回归分别集成于 `6a5e052c4`、`0614d9d39`；其余夹具与补充回归随主提交保存。无迁移、依赖、环境配置、镜像构建或发布版本变化，数据库保持0231。

- 托管图片预检沿canonical原件保存位置完整读取、核对SHA／大小并解码，安全身份收据进入新快照和摘要，不再把当前桶当输入证据。HTTP使用准确原请求并在断连时取消、排空；后台保存原Key不可逆指纹，继续核对原创建者、manager权限及原两个读取scope，不增加tasks:read／datasets:write。
- prepare独立拥有读取事务，finalize在调用方短事务内重核选择、标签与原件，不隐式提交调用方。自动策略重取锁后比较完整上下文；种子成功写入后登记真实Asset／Replica，首次组合分割种子先明确提交完整来源阶段，再重取锁、刷新状态并延续原编辑保护，避免独立mask owner被未提交的新绑定阻塞。
- Worker优先使用固定输出owner的保留媒体，新合同独立核对冻结原件SHA／大小，改写对象metadata不能替换输入。只有缺失副本才完整核验回源；旧无身份且副本缺失明确拒绝，已有合格历史副本保持。ready不重选当前标签，本次真正回源的完整来源及Gate才加入末端复核。
- 图片、mask及ZIP写入后完整读回校验，所有原生工作、文件及临时目录实际收尾后再发布。原件使用独立两槽，未知关闭保留相应并发额度及耐久责任；重试／删除受执行锁及未结输出操作保护。Worker、取消和训练信号均按Version→Job锁序；信号另核对真实任务参数、attempt、Job及Celery ID，旧信号不能终结新尝试。引用关闭时的重复取消也保留先前未知输出责任。

最终累计476个不同本地后端用例通过，无剩余失败或跳过；按最终同名用例去重，不累计重跑。包含97项真实图片／物理生命周期、45项信号回归，以及独立事务中解码和真实关闭两阶段的账号、owner、Task、尺寸、Asset删除、Replica与Gate变化，证明来源／权限锁不跨物理工作。覆盖创建末端标签变化、准确双读取scope、同ID Key轮换、HTTP断连排空、无调用方commit、输出完整读回与篡改、旧缺副本零raw I/O、真实数据库执行围栏、未知操作阻止重试／删除、策略并发与首次分割种子的跨连接可见性。

独立18100／13100实际跑通HTTP→MinIO→本checkout Celery的检测、原生RLE两类构建，各三图，外部完整下载ZIP核对SHA与成员；随后删除当前源Task、标注和原图，两类重试均ready，冻结manifest、snapshot摘要及输出owner保持。未运行ML训练或GPU资格验证。浏览器确认就绪列表、洞察、冻结样本和删除源Task后的真实图片预览；已查看明暗与390px窄屏截图，无页面异常、失败请求或页面横向溢出。本片未修改UI样式。

README、CHANGELOG Unreleased及用户／API／架构资料同步；OpenAPI快照无漂移，docs build、Ruff／format、正常提交hooks与diff检查通过。人工Worker确认idle后经原监督服务刷新，两个节点各72任务注册、职责队列及实际无业务owner的训练派发核验通过；绑定Beat随服务恢复并确认使用本checkout dev模式。3100／8100均200，11表完整行指纹、24对象完整SHA及12源文件与本片前一致。

test／e2e操作账本均为零；两个一次性数据库、两个Redis和18桶销毁后独立查验不存在。自有浏览器关闭，六棵已集成且干净的隔离树及八个patch等价临时分支移除；本轮测试／文档构建缓存、截图、探针与私有报告按归属清理，保留人工环境、虚拟环境、共享依赖、生成类型和工作树身份。完整E02／B13与整套epic仍在执行，不归档活动计划，完整用途切换保持关闭。

## 下一片范围 — 图像金字塔原件与派生产物归属

只读盘点确认：image_pyramid的owner_for_task／owner_for_dataset_item仍按现行annotations／datasets用途桶组装ImagePyramidOwner，下载仅在本次读取后生成指纹；金字塔manifest／tile写入和清理沿现行media_cache_bucket。下一片先定稿canonical原件身份、固定派生输出owner、lease／取消与真实生成收尾、旧manifest兼容及最终发布事务，再实施。不能仅替换下载GET，也不能把现行缓存桶当旧产物归属或将派生指纹当已登记原件证据；现有目录／引用输入限制与渲染资格边界先保持。该消费者尚未迁移，其他LiDAR导出与视频派生链继续独立推进。

### E02-B13-B3-E — Design

图像金字塔使用统一原件 resolver 的已登记 SHA、长度、位置和 Task／Item 关系。准备阶段仅冻结元数据；下载使用条件请求和全量 SHA／长度核验，保留金字塔现有资源预算。HTTP 入口保持 annotations:read 与现行 Task 可见性，保存不可逆凭据指纹及准确原件委派；后台重新检查当前授权。内部上传、导入和维护入口必须显式委派，不将缺失 HTTP 凭据视为系统授权，不对已归档数据集额外限制读取。目录和原地引用仍拒绝。

新增独立 ImagePyramidOutput 持久责任，不随业务对象级联删除。每次租约分配独立 UUID 输出前缀，保存分配时的 media-cache 位置和端点；后续读取、签名、删除不选择当前用途桶。完整回读每个 tile、overview 和 manifest 的 SHA／长度后发布私有清单；公开 manifest 继续既有 schema。统一 Asset→Generation→Output 锁序，发布重新检查原件、准确委派、未到期租约、输出 owner 与 Gate，原子切 active。旧尝试不得删除新尝试的独立前缀。

源读取、原生生成、输出回读和关闭均持有 durable StorageOperation；取消和超时等待真实工作及文件关闭，未知收尾保留责任。GC 先在短事务退役已知 owner，再在 Gate 准入下按保存前缀删除；业务删除只移除业务引用，独立 owner 留待 GC。停止裸扫现行缓存桶和按时限删除其他任务临时目录。旧无 owner 的代不静默采纳、签名或删除，用户可在原件已登记后显式重试新代；旧未知文件保留待单独核验。

专用 API、Task／Dataset／Dashboard 缩略图、列表摘要、自动分流、CLI 和删除入口一起迁移。HTTP 缩略图回填不隐式升级成系统金字塔委派。验证使用独立 test／e2e 环境，覆盖真实原件与输出篡改、凭据／关系变化、租约并发、物理 drain、旧数据拒绝、GC 和业务级联；保留人工验收 3100／8100、两 worker、Beat 及原业务数据，结束清理本片产物。实施和验证完成前不宣称本片、整个 B13 或完整用途切换完成。

## E02-B13-B3-E Outcome — 图像金字塔原件与独立输出

主集成 `f3d9bb80e`，独立输出模型／存储与回归分别为 `e25541c8b`、`5e063a8b9`，原件／授权与 HTTP 回归为 `5510057df`、`13ae56a49`、`918728acc`、`9db88f67a`，消费者迁移与关闭责任修复为 `01671926a`、`ae38cd0f7`、`3d21af9c5`。新增迁移 0232；不增加依赖、配置或发布版本，不重建镜像。

- 准备只冻结 canonical 原件元数据；生成沿登记位置条件读取并完整核验 SHA／大小。HTTP 保持 annotations:read 和现行 Task 可见性，冻结准确原请求／Key 指纹及 Task 关系；后台入口显式委派，缺少凭据不升级为系统权限。所有交付与发布在物理工作结束后重新核对原授权、关系、原件及 Gate。
- 每轮租约独占一个 UUID 输出前缀，保存 media-cache 位置／端点和完整对象清单。tile、overview、manifest 全量回读通过后才能发布，读取、缩略图和清理不随当前用途桶漂移。统一 Asset→Generation→Output 锁序；旧尝试不能发布或删除新尝试的输出。原生解码、线程、Body、client 和临时文件必须实际关闭，未知收尾继续保留许可和耐久责任。
- 业务删除保留独立 output 责任。GC 排除在用操作后退役已知 owner；已发布输出从退役时重新等待宽限期，再按保存位置删除。删除失败保留重试，不裸扫缓存桶或其他任务临时目录。旧无归属代明确拒绝签名／采纳／删除；原件登记后可显式重试新代。目录／引用限制、原预算和公开 manifest schema 保持，完整 media-cache 用途切换仍关闭。

最终 316 个不同本地后端用例通过，无剩余失败或跳过，按最终同名用例去重。覆盖真实数据库级联／独立责任、当前桶陷阱、完整对象篡改、租约及输出不可变约束、晚到撤权／同 ID Key 轮换、关系变化、取消／软时限与未知关闭、无调用方 commit、旧数据拒绝及已签 URL 的 GC 宽限期。Ruff／format、正常提交 hooks、OpenAPI 无漂移、docs build 和 diff 检查通过。

独立环境实际跑通 HTTP→Celery→MinIO：4097×4097 图片生成 129 块 WebP tile，外部完整读取并核对全部 131 个对象的 SHA／大小，所有 WebP 可解码，原图 SHA 保持；批签去重及条件 GET 304 通过。原生生成使用已有镜像中的 libvips 8.14.1，未安装软件或构建镜像。浏览器实际核对 Data Manager 缩略图／预览与工作台瓦片、缩放，查看明暗桌面及 390px 窄屏截图，无页面异常、捕获到的失败请求或页面横向溢出；控制台仍有既有 Konva 六层画布建议。本片不授予硬件性能资格。

人工两个 Worker 确认 idle 后通过原监督服务更新，迁移到 0232；两个节点各注册 72 项任务、职责队列和新签名实际派发检查通过，缺委派的无业务 owner 探针明确拒绝物理工作。绑定 Beat 恢复，3100／8100 均 200；11 表 78 行完整指纹、24 个对象完整 SHA 和 12 个源文件与更新前相同。宿主机缺少 libvips.so.42，人工 host Worker 的原生生成能力仍未资格化；上述真实生成验收来自独立已有镜像，不能混同。

test／e2e 的 StorageOperation 均为零，两个数据库、两个 Redis、18 桶销毁后独立确认不存在。专用 Worker／API／Web 和自有浏览器关闭，核对九个来源提交 patch 等价后，清理四棵已集成且干净的隔离树及四个临时分支；本轮私有探针、截图、测试／文档构建缓存按归属清理。保留人工环境、虚拟环境、共享依赖及工作树身份。完整 E02／B13 和整套 epic 继续执行，不归档活动计划。

## 下一片范围 — 视频派生媒体的原件与输出归属

只读复核确认，浏览器原生视频已使用受限原件读取，但 `media.py` 的视频元数据／转码／poster 和 frame／chunk 派生链仍存在现行 datasets 桶回源及 media-cache 桶写入／签名。下一片先定稿 canonical 视频输入、独立派生 owner、ffmpeg／临时文件的真实收尾、持续授权、发布与旧缓存兼容，再实施对应消费者；不能把原件 SHA 当作派生物内容证据，也不能仅替换下载 GET。LiDAR 导出、其余 ML／导出消费者和完整用途切换继续独立推进。该后续目前只有只读盘点，尚未实现。

## E02-B13-B3-F Design — 视频派生媒体的统一归属与发布

本片覆盖现有 direct DatasetItem 视频的 metadata／timetable、转码／poster、frame／chunk、内部预标读取、CLI、缩略图和 TTL 清理。独立 TaskOriginal 原生播放与现行受限原件会话保持；当前不支持的独立 Task 派生能力不冒充已迁移。源始终是 canonical 已登记视频，不用播放转码版抽帧。来源收据固定原件与 Item／Task 关系；视频生成会更新的派生尺寸不当作原件内容身份，其他身份事实仍完整重核。目录／引用继续拒绝。

新增独立 VideoDerivativeOutput 责任行，每次尝试使用新的 UUID 输出前缀，保存 Item 身份、种类／规格、准确原件／委派、metadata generation、lease、固定 media-cache 位置／端点、结果及完整 SHA／大小清单；不设业务级联 FK。Item、FrameCache、Chunk 的当前指针引用该责任行。metadata 结果与时序一次提交，frame／chunk 必须固定当前 metadata generation，不能跨原件、时序或参数复用。已发表结果／清单和来源／授权／位置事实不可变；旧尝试不能覆盖新指针或新前缀。

HTTP 委派保留具体 Task 和准确原请求／Key，不新增现有读取入口没有的 annotations:read scope，不改变 Task retry 与 facade retry 的现有差异或归档数据集读取规则。Dataset 管理和存储维护入口保留自己的角色／owner／生命周期合同。内部导入、种子与维护必须在明确的服务入口显式系统委派，缺请求不自动升级。当前读者可以复用已核验的共享输出，后台生成仍沿冻结委派持续复核。

原件完整条件读取、SHA／大小核验与独立两槽 admission 使用现有 reader 和 durable StorageOperation。ffmpeg／ffprobe 显式继承匿名 stage FD，避免再复制整个视频；取消、软时限、超时等到真实子进程、管道、输出／源文件和 client 关闭。未知收尾保留许可及责任。全部输出条件独占 PUT，完整读回后在短事务按 Item→Frame／Chunk→Output 锁序，复核准确授权、原件／metadata generation、当前指针、有效 lease 和源／目标 Gate，再原子发布。元数据／时序不再随 poster pending 提前提交。

ready 交付沿保存 owner 完整核验实际对象并末端重核当前读者；DTO、samples、字节与浏览器 decoder／缓存共同使用同一输出 UUID／SHA。旧无事实产物明确等待重新生成，不猜当前桶或回填 owner；旧未知对象不盲删。业务删除和 TTL 只解除引用，已知独立 owner 无引用／在用操作后退役，已发布输出从退役起等待签名宽限期，再按保存前缀删除；新前缀避开旧 videos/ 自动过期规则。

实施在基于当前 HEAD 的隔离树中进行，验证使用新 test／e2e 模式、实际 ffmpeg／MinIO／HTTP／Celery、精确帧／packet 样本和浏览器；不重建镜像或安装依赖。保留人工 3100／8100、两 Worker／绑定 Beat、既有业务行／对象／源文件指纹。同步 API／用户／架构文档及生成合同，完成真实验收与清理后记录 Outcome；完整 E02／B13、用途切换及整套 epic 仍继续执行。

## E02-B13-B3-F Outcome — 视频派生媒体的独立责任与完整发布

主实现 `ee14ac98b`，原件／模型／存储／API／消费者及真实种子提交一并集成；浏览器代际收尾为 `75a567b79`，短锁恢复为 `9d3962d4e`、`032657b25`，批量准备锁窗口为 `151bb1483`。新增迁移 0233，OpenAPI 快照与本工作树生成类型同步；不增加依赖、配置或发布版本，不构建镜像。

- direct DatasetItem 视频沿 canonical 已登记原件完整读取并核对 SHA／大小，HTTP 固定具体 Task、原请求／Key 证明及原有可见性；内部导入、种子、维护和帧预标明确委派。原件尺寸与 Task 关系分开核对，派生尺寸更新不会被误认成原件替换，其他来源／授权事实仍持续复核。
- 每次尝试保存独立 VideoDerivativeOutput、UUID 前缀、固定 media-cache 位置、规格、metadata generation、一次租约和不可变结果／对象清单。metadata、完整时序、封面与活动指针在生成、全量回读和真实关闭之后一起发布；frame／chunk／samples、签名、预标、CLI 和缩略图沿同一输出身份。浏览器核对字节 SHA／大小，缓存、decoder、预览及迟到结果按账号、Task、generation 和输出隔离。
- ffmpeg／ffprobe 只继承已核验匿名文件 FD；源、原生子进程、管道、文件、输出 client 与临时目录真实收尾后才能交付。重复取消、软时限或未知关闭保留实际责任；不使用硬时限把在途工作当成结束。业务删除／TTL 先解除引用，已发布输出从退役起重新等待签名宽限期，再按保存前缀清理；旧未知文件不猜桶、采纳或盲删。
- 真实浏览器发现并修复短锁导致清单停止等待、分块立即降级的问题：仅三种明确的锁忙 409 重试三次，其他权限、来源和代际错误直接失败。批量帧、requested／warmup chunk 每个准备后释放写锁，保持末端整批来源／授权／generation 复核及成功后统一派发；中途失败保留已提交未认领责任，不派发旧代。

最终 542 个不同的当前后端用例通过，无剩余失败或跳过；最后一轮 122 项覆盖新独立连接取锁、API、兼容入口及真实派发。旧预算为 900 时的无效参数 901 已被当前固定预算 1800 的边界 1801 替代，未将失效旧失败冒充成功重跑。前端按每文件最终用例集统计 10 文件 208 项通过，整站 TypeScript、web lint／CSS tokens、正常提交 hooks、OpenAPI／生成类型、docs build 及 diff 检查通过；未运行远端 CI。

独立环境实际跑通 HTTP→本 checkout Celery→MinIO：metadata／poster、帧、分块及预标按生产链生成，外部完整下载核对 SHA／大小和冻结身份，过期输出 ID 返回冲突。原生样本校验覆盖 B 帧、精确索引、VFR 与跨 GOP；损坏 codec／samples 只在浏览器响应层注入，持久化夹具仍由真实生成与发布链建立。

Chrome 150 严格 WebCodecs 套件 9 项全部通过；另用 180 帧、三个分块的视频验证 F1／F62／F122／F171 的实际画面标记，切题返回及重新加载恢复正常。已查看 1440×1000 桌面与 390×844 窄屏的明暗截图：窄屏保持桌面操作提示，无页面横向溢出；无未处理页面异常，控制台保留既有 Konva 六层建议，捕获到的短锁 409 已用于上述修复。实际图形后端为 SwiftShader、WebGPU adapter 不可用，本片不授予硬件解码或 GPU 性能资格。

人工两个 Worker 确认 idle 后由原监督服务更新至 0233，各注册 73 项任务，职责队列及新任务无业务输入派发检查通过。绑定 Beat 恢复并核对同工作树 dev 模式，8100／3100 均 200、doctor healthy；12 表 155 行完整指纹、24 对象完整 SHA 和 12 源文件与本片前一致，新输出表与新 Item 指针保持为空。人工 host 的 libvips 既有缺失未在本片修复，不混同为金字塔原生资格。

test／e2e 的 StorageOperation 均为零后，两个数据库、两个 Redis 与 18 桶销毁，并分别查询 PostgreSQL、Docker 与 MinIO 确认不存在。专用 Worker／API／Web 和自有浏览器关闭，来源提交 patch 等价核对后清理五棵已集成且干净的隔离树及分支；本轮私有报告、截图、探针、测试／文档构建中间产物按归属清理。保留人工环境、虚拟环境、共享依赖、生成类型与工作树身份。完整 E02／B13 和整套 epic 仍继续执行，不归档活动计划，完整用途切换保持关闭。

## 下一片范围 — LiDAR 导出原件与交付媒体

只读复核确认 KITTI、pointmask 和 COCO-multicamera 的 ZIP 已有独立 ExportOutput，但包内媒体地址仍来自当前 datasets 桶；部分读取只按 Item 大小、对象 metadata 或本次现算 SHA 判断来源，主点云 direct／primary 定位也不一致。下一片先冻结 canonical 主点云与精确相机链接、坐标／标定／标注内容，并将实际媒体副本归属与 ZIP 发布串起完整责任链，不能仅替换 GET 或直签当前桶。

拟采用独立 ExportMediaBundle 在任何副本 PUT 前登记责任，保留现有 ZIP、manifest／路径及下载脚本格式，媒体链接指向固定输出位置的原字节副本。以 bundle 创建时固定七天截止，签名不对齐延长，ZIP 与缓存不得续期；读取、复制、完整回读和真实关闭后，在同一最终事务重核授权／来源／scope 并发布。同步预检与项目／批次权限、预算、旧缓存兼容及失败回收须在 Design 中定稿后实施。nuScenes raw lidar.bin／map 缺少独立辅助原件归属，另片处理，不能当作 PCD 的副本；其余 ML／导出消费者和完整用途切换继续独立推进。此后续目前为已复核范围，尚未实现。

## E02-B13-B3-G1 Design — LiDAR 导出原件与独立交付副本

本片覆盖 KITTI、pointmask、COCO-multicamera 的项目／批次预检、Job 冻结、Worker、打包、缓存、发布和回收。保留现有 ZIP 路径、坐标转换、类别映射、持久人工多相机框及负样本；已有 stale 校准关系的人工框仍可导出，不新增“成员必须使用最新校准”的条件。KITTI 读取主 PCD 与选定相机，pointmask 读取主 PCD 及现有图片清单的所有相机，COCO 只读取全部相机，不强迫取得缺失的主 PCD。主件统一 `resolve_task`，相机使用准确 `resolve_task_link`，拒绝 direct／primary 冲突和未知原件，目录／引用不在本片增加支持。

权限沿用项目预检的 export.annotations、批次预检的项目可见性，以及两类实际导出的 export.annotations；不增加 tasks:read／annotations:read，也不对 whole-project／batch 额外逐题过滤。selected-task 的场景格式拒绝保持。新 Job 固定原 output_authority 的准确凭据证明，重发不能续换原 Key 或从缺请求推定系统授权。本片三类目标的同步预检只核对登记事实、格式配置和预算，不读取原件字节；实际存在性、SHA 和 PCD 完整校验在 Worker 执行。

HTTP 在同次短事务冻结 project／batch、明确有序 Task 集合、规范化 targets／options、命名、类别／属性、Task／link、标注／多相机成员／轨迹、Dataset 坐标约定、所用校准及 canonical 原件事实。先计量数量和 JSON 体积，再分块冻结，不能先加载无限输入再检查预算。冻结内容保存在私有 bundle，不把凭据或完整内部来源写进公开 Job DTO；Job 仅保存新合同、bundle ID 与摘要。Worker 必须将实际消息参数与原冻结范围逐项比较，不能按 Celery 参数重新扩大或改写 batch。每个物理对象前后复核本对象、当前发起者／项目／原 Key、Job／attempt 和 Gate；整套输入在 Worker 开始、缓存命中与最终发布时复核，避免每对象重新扫描全量范围。

迁移 0234 新增独立 ExportMediaBundle：保存 project／job 快照、预分配 zip_output_id、私有输入及摘要、逻辑路径映射、预期对象 UUID／SHA／大小清单、固定 export 位置／端点与专属前缀、预算、状态、一次 lease 和时间事实，不随业务对象级联。精确副本的 SHA／大小在 PUT 前已知，预期清单自创建起不可变；claim 只允许一次，不续约或接管旧租约。Worker claim 后租约固定 3600 秒，未完成的物理工作仍须排空，过期不能发布。重复投递不获新写权限；显式新尝试使用新 bundle／前缀，保留原输入和授权，旧任务不能改变新 Job 指针或终态。

ExportOutput 保持现有真实 ZIP SHA／大小及 image 前缀，新增唯一 nullable bundle 关联，按预分配 UUID 创建；同事务延迟约束核对 project／job、精确 ZIP ID、共同截止时间与首次 ready 的配对状态。旧输出保持 NULL，不猜测回填。本片涉及三类媒体的旧缓存必须 miss；新缓存键包括新合同与完整输入摘要，读取完整核对 ZIP 和所有副本，截止时间不续期。媒体 bundle 的 published_at 一次锁存，不能因 ZIP／cache／Job／项目删除而丢失。

bundle 登记时固定 created_at＋168 小时，所有副本在登记后写入；签名使用剩余秒数、align=False，ZIP 和 Job 交付期限继承同一截止，避免先写副本被 export 桶既有七天 lifecycle 提前回收。KITTI／pointmask 清单保留原字段并补充真实 SHA／大小，下载脚本核对精确副本；COCO 延续其已有哈希校验。复制逐字节保留原图和 PCD，不转换、重排或采样；物理 key 只由登记的 UUID 组成，用户逻辑路径只存在清单。

源捕获使用已登记保存位置、条件读取及完整 SHA／大小，独立两槽覆盖真实 stage 使用至关闭。输出 client、PUT、全量回读、Body、文件和实际 close 均在 bundle 的耐久许可内；所有 BaseException 等待物理线程真正结束，未知构造／关闭保留许可、owner 和必要临时文件。预期清单每项只验证一次写回，最终线性核对前缀集合，不在每次 PUT 前遍历全部已写对象。来源、输出、ZIP 的物理工作全部在事务锁外执行。

最终保持取消／授权的既有锁顺序，范围沿 Batch→排序 Task，使用 Task 写锁阻止新增标注／链接，继续核对 Annotation／成员／Track；校准沿 Dataset 生命周期→Item 父行锁后重算事实，不能只锁已有历史行。跨序竞争使用 NOWAIT，冲突拒绝本次发布，不靠长时间等待带着其他锁执行。自然期限与 Gate 在签名／ready 前再检查，bundle、ZIP、cache、Job 与通知一起提交；来源、权限、标注、坐标、校准、消息范围或代次变化都不能发布半轮结果。

预算按实际所需源去重：相机保留 20000 图、100000 人工成员、256MiB／图及20GiB 合计；PCD 上限1000帧、256MiB／件、4GiB 合计、4000000点／帧及100000000点／次；两类媒体累计不得超过24GiB。私有冻结 JSON 最多256MiB、点索引总数最多32000000，原有8GiB临时文件／512MiB单 ZIP entry／200000 entries限制继续执行。逐项捕获、校验和复制，不同时下载全部媒体；未结 building 副本仍计入预算。数值限制是资源合同，不冒充实测 RSS 或吞吐资格。

pointmask 以原 PCD 记录空间生成恰好 N 个 little-endian uint32：不删除 NaN／Inf、不乘抽样 stride、不根据注解声明扩容。支持 ASCII、binary 与 binary_compressed；增加有界 header／逐行格式检查、精确体长及流式 LZF 校验，不安装依赖或构建 XYZ 全矩阵。实际记录数须匹配 POINTS 与 WIDTH×HEIGHT，压缩数据须完整消费、回溯合法并达到精确解压长度。索引严格为非 bool 整数且落在 [0,N)，已有 source_point_count 必须等于 N，未知类别拒绝。保持现行 created_at／id 顺序的后写覆盖，冻结该顺序；标签使用有界 uint32 缓冲，避免 Python 指针数组和参数展开。

回收独立于业务与缓存：published_at 非空的媒体在固定截止前不删除；未发布候选只在 attempt 已结束／失效且没有在途或未知操作后退役，pending ZIP 同样受限结算，双方删除失败分别保留责任。只处理保存端点、桶和准确 UUID 前缀，不扫当前桶、未知对象或其他临时目录。墓碑默认保留。恢复映射仅通过现有 owner／restore guard 修改 bucket 和 storage_identity，其他输入、清单、deadline、lease 和历史事实不重写，并加入恢复一致性与真实副本核验。不可变 ZIP 内旧签名 URL 不会随桶映射更新：恢复责任不冒充旧链接迁移；旧 cache 失效，新桶交付须新责任生成 ZIP／URL。

格式组合不因本片被禁止；与 nuScenes 组合时，本合同覆盖上述三类媒体及其 canonical 共同来源，raw lidar.bin／map 仍属于未迁移的独立辅助原件链，不宣称组合 ZIP 已实现全媒体闭环。nuScenes 单独输入、其他 ML／视频导出及完整用途切换仍另片推进。

实施使用基于本地 Design HEAD 的隔离树、新 test／e2e 和真实 PCD／图片／MinIO／HTTP／Celery。验证原件／副本篡改、当前桶陷阱、primary／camera 定位、三种 PCD 与点序、缓存／消息／标注／校准／Key 变化、租约并发、真实取消收尾、回收、恢复映射及三格式实际 ZIP 下载；浏览器检查预检与下载。保留3100／8100、两 Worker／绑定 Beat和12表／24对象／12文件指纹，完成后清理本片产物；不安装依赖、重建镜像、发布版本或宣称整套 epic 已完成。

### G1 implementation checkpoint — frozen serialization

三类目标的登记输入已落在独立 bundle；实际打包使用脱离 ORM 的冻结项目、题目、标注、关联、标定和轨迹事实，构建过程中不再重读业务行。混合 AAP JSON 同步冻结预测及场景轨迹；混合 NuScenes 另冻结完整场景成员，并只复用规范主 PCD 的独立副本，raw lidar.bin／地图仍归 G2。相机名称或帧标识造成生成的标注／标定路径冲突时，在入队前明确拒绝，不覆盖已有成员。ZIP 预分配归属复用 bundle 保存的桶与端点，不从当前用途默认值另分配。

当前验证：隔离 test 数据库上的 294 项预检、输入、冻结打包、媒体路径、传输、下载及回执测试通过，无跳过；此前底层模型、物理存储和 PCD 检查 229 项通过。Worker 发布、缓存、回收和真实恢复仍在集成中，此检查点不代表 G1 Outcome，也未刷新人工验收环境的 0233。

### G1 implementation checkpoint — delivery, cache and recovery

Worker 已接入一次 claim 的独立副本流程；实际冻结输入再次执行业务预检，防止 HTTP 预检与登记之间的修改绕过多相机父成员规则。缓存从保存位置完整验证 ZIP 和全部媒体，发布前核对同一原请求凭据及当前冻结范围，复用首次截止。媒体／ZIP 的退休与未知操作保护已接入现有清理任务；并发迟到或重复投递不能改写胜出任务。

无锁 canonical 读取使用来源标量投影、累计 links 预算、最多两条候选副本、ETag 字节上界及位置配置 SQL 比较，避免先载入大型 JSON／字符串再拒绝。该限制约束应用侧返回数据，不宣称限制 PostgreSQL 内部 TOAST 或比较成本。交付链接遵循公共存储网关，三类下载脚本支持显式 `--base-url`，保留签名路径与查询；NuScenes 辅助文件下载器仍保持其既有合同。

真实 ASGI HTTP、隔离 Redis/Celery 与 MinIO 验证完成五个任务：项目／批次三目标导出、固定截止的缓存命中，以及原件同长度篡改与入队后撤销原 Key 的明确失败。空 scopes Key 沿既有导出能力成功，不新增 tasks:read／annotations:read。当前 datasets 桶放同键错误内容，原件登记在另一个拥有桶，实际仍读取正确保存位置。七条媒体引用完整下载核对 SHA／大小；PCD 含 NaN 且 metadata 故意写 999，实际三条记录生成精确 12 字节 label。公共网关下十次生成脚本下载验证通过，读取许可归零，测试 Worker 已退出。API 请求经 ASGI 执行，未将其表述为另一个实际 TCP API 服务的资格。

真实离线演练已通过：源环境先销毁，165 张表／172 行及九桶 72 对象在新随机目标恢复；两个 bundle 的四个 PCD／PNG 副本与两个 ZIP 完整核对，旧媒体 URL、期限和冻结事实不变，准确的 G1 缓存索引失效。演练仅合成离线资格，不提供生产 RPO／RTO；两套随机环境已验证清理。修正旧训练夹具的显式内部授权调用，并冻结源公共 URL 路径前缀用于恢复核验。

产品与 API／架构说明、CHANGELOG 已同步；docs build、OpenAPI snapshot check 和 codegen 检查通过。最终汇总回归、主工作树合入、0234 人工环境刷新及数据指纹复核仍待完成，因此此处仍不作为 G1 Outcome，也不归档整项 E02／B13。

## E02-B13-B3-G1 Outcome — LiDAR 独立媒体交付

主集成与最终回归 `570f3f2a8`；冻结序列化为 `772b9f28a`，退休与恢复为 `46d9c6182`、`41820c005`，有界来源读取与下载 origin 为 `e03d63206`、`d2e2db3fe`。迁移 0234 新增独立 ExportMediaBundle，与预分配 ZIP 配对发布。KITTI、pointmask、COCO 多相机的实际原件沿登记位置完整核验，冻结业务输入、准确请求授权及一次租约；精确媒体副本、ZIP、缓存和 Job 继承共同七天截止。来源／权限／消息／标注／校准变化拒绝发布；未知物理工作保留耐久责任。

最终按当前用例集汇总，29 个相关后端测试文件中的 1317 项均已通过，无未验证用例；恢复工具另有 73 项 unittest 通过。覆盖模型约束、三种 PCD 与原始点序、当前桶陷阱、完整对象篡改、冻结与预算、授权变更、取消排空、缓存、退休和恢复。正常提交 hooks、Ruff／format、OpenAPI 无漂移、codegen、docs build 及 diff 检查通过；未运行远端 CI。

真实 ASGI HTTP→隔离 Celery→MinIO 完成五个任务：项目／批次三格式导出、固定截止缓存复用，以及同长度原件篡改和入队后撤 Key 的明确失败。七条媒体回执完整核对 SHA／大小，十次生成下载脚本实际读取公共网关并核验；三条含 NaN 的 PCD 记录生成精确 12 字节 label。ASGI 请求不冒充独立 TCP API 服务或本片浏览器验收。

真实离线恢复先销毁源，再恢复 165 表／172 行、九桶 72 对象；两个 bundle 的四个 PCD／PNG 副本及两个 ZIP 内容、冻结事实和共同截止保持，准确失效含历史链接的 G1 缓存。旧 ZIP 中签名链接保持历史值，不宣称已迁移链接或生产 RPO／RTO。两套随机恢复环境已销毁并查验。

人工环境经原监督服务刷新到本 checkout 与 0234，两 Worker 各 73 项任务注册、职责队列及新清理分支实际派发通过，绑定 Beat active，3100／8100 均 200。12 表 155 行完整指纹、24 对象完整 SHA 和 12 个源文件与本片前一致；清理后的最终复核仍相同。未重建镜像、安装依赖、修改共享配置或分配发布版本。

test 数据库、Redis 和九桶已销毁并独立确认不存在；临时 Celery 全部退出，读取操作归零。六棵干净隔离树的来源提交均核对 patch 等价后移除，六个任务分支及本片测试／构建缓存、私有探针和下载产物按归属清理。保留人工环境、虚拟环境、共享依赖、生成类型及工作树身份。

用户资料已同步到 `docs-site/user-guide/reference/export-formats.md`、`docs-site/user-guide/projects/batch.md`，API 与架构资料位于 `docs-site/api/` 和 `docs-site/dev/concepts/`，README 与 CHANGELOG Unreleased 同步。NuScenes 的 raw lidar.bin／map、独立 NuScenes 导出及其全部相机仍待 G2；完整 E02／B13、完整用途切换与整套 epic 均继续执行，不归档本活动计划。

## E02-B13-B3-G2 Design — NuScenes 原始传感器与地图归属

本片将纯 NuScenes 和所有混合格式一起接入 G1 的冻结输入、独立副本、固定期限、缓存和恢复合同。原始五通道 `.pcd.bin` 与平台坐标变换后的 PCD 是不同原件；NuScenes 清单必须交付原始 sensor 字节及全部已链接相机，平台 PCD 仅用于已有精确点框统计。保留完整场景、sample／sensor／log／map／pose、原 filename 和坐标合同，不能用主 PCD 替代 raw，也不能只更换现有 URL。

迁移 0235 增加独立 DatasetAuxiliaryOriginal 当前归属：id 对应唯一 verified Creation，保存 dataset 快照、raw_lidar／map 种类、位置、端点身份、桶、对象键、真实 SHA／大小、写回执和时间。业务父行不级联删除此责任；DatasetItem.nuscenes_raw_original_id、Scene.nuscenes_map_original_id 为可空 RESTRICT 指针，绑定检查精确 Dataset／种类／Scene／主件关系。共享地图可以引用同一已知 owner，不按 SHA 猜测合并。不增加可见假 Item 或把不同内容当同 Asset 的副本。

Creation 新增 dataset_auxiliary 范围和专属 `__aap_auxiliary_originals__/{dataset_id}/{creation_id}/payload`，复用现有条件写、完整回读、Gate 和耐久账本；专属空间禁止普通浏览器 PUT，且不进入 Dataset 自动扫描。新导入与明确重导的 raw／map、平台 PCD／相机都走受控创建与真实登记，幂等已有归属先完整核对后复用，不能覆盖稳定键。原始点云一次有界读取后同时生成 raw 收据及平台 PCD，避免两次打开读到不同文件。已有 JSON 指纹不构成归属；不从当前桶 HEAD 猜测补登记。取消、软时限和其他 BaseException 必须等待整个创建及真实关闭完成，调用方源文件随实际工作存续；未知关闭继续保留责任。

独立辅助 resolver 不授予新权限，只在原导出授权的准确 Task／primary Item／Scene 上核对指针、Dataset、原件与保存位置，返回统一 OriginalSource，复用现有两槽、条件读取、完整 SHA／大小和真实排空。全部相机使用 canonical Task link。冻结包含辅助归属、指针和完整来源合同，HTTP 预检仅核对登记事实、规则和预算，不访问对象存储；实际消息、原 Key／账号、Job attempt、每件原件及最终全量冻结输入继续复核。现有项目／批次预检权限差异保持，不新增 datasets／tasks／annotations scopes。

统一 bundle 扩展 raw_lidar／map 覆盖和 NuScenes 路径，每个清单引用必须精确对应已登记副本及原始逻辑路径，路径冲突核对完整来源而非单一 SHA。纯 NuScenes 不再走旧缓存；混合 KITTI 也收齐 NuScenes 所需全部相机。新 schema／缓存合同不采纳旧无辅助事实的结果，已登记 G1 历史责任仍可读取、退休和恢复。ZIP、所有副本与 Job 仍共同使用创建起七天截止，首次期限不因缓存命中、业务删除或恢复续期；NuScenes 下载脚本并入公共网关 origin 与完整哈希校验。

资源合同继续包括 256MiB／对象、20000 图、1000 主 PCD、图像20GiB、主 PCD4GiB、冻结 JSON256MiB、ZIP8GiB／单成员512MiB／200000成员；raw／map 同样计入总计21000对象及24GiB，不得作为未计量辅助资源。NuScenes 1000帧、30000框、1亿点框测试等现有预算保持。收集、冻结和辅助查找先作数量／字段体积边界，不在载入大 JSON／字符串后才拒绝。

辅助原件沿当前保守原件保留政策：业务删除只解除指针，独立 owner／Creation 和未知操作责任保留，不宣称已有自动物理回收资格。七天交付副本继续由 G1 精确清单退休与回收；不增加裸扫或按当前桶删除。恢复只在现有 restore guard 下映射辅助当前 owner 的 bucket／storage_identity；历史 Creation、冻结 JSON、签名 URL、期限和所有内容证据不变，新增语义核对与 raw／map 真字节夹具。新 bundle 沿用精确缓存失效；旧 ZIP 的历史链接不能因桶恢复被宣称已更新。

实施在本地 Design HEAD 的隔离树，测试只用新建可销毁环境，覆盖新导入／幂等重导、指针错配、共享地图、扫描与直传禁区、源篡改／撤权、纯及混合导出、全场景与坐标合同、预算、真实关闭、恢复映射和清理。同步用户／API／架构资料与生成合同。保留3100／8100、两 Worker／绑定 Beat与既有业务指纹，不安装依赖、重建镜像、分配发布版本或归档仍活动的 E02／B13。

### G2 implementation checkpoint — auxiliary originals and complete delivery

0235 的独立辅助归属、数据库绑定约束、专属创建空间及 raw／map resolver 已落地。导入器从同一有界原始读取生成 raw 与平台 PCD，受控创建及完整核验在业务最终锁外完成；已登记重导复用原件并核对旧元数据和当前关系，最终事务检查 Dataset／Scene／Item 与准确委派授权。物理线程在所有 BaseException 下排空并采纳迟到回执；未知收尾保留耐久许可。

新输入合同为 schema 2，纯 NuScenes 和混合格式同样冻结完整 Scene、全部相机及辅助原件，逐件复制、缓存核验与发布复核沿用 G1。共享地图虽只写一个副本，每个引用 Task 都独立检查绑定。生成下载器完整校验大小、SHA 和安全路径，支持公共网关及显式 base URL。空 raw `.pcd.bin` 保留零字节与空内容 SHA；迁移 0236 仅放宽不可变预期清单的零字节下界，应用仍拒绝空图片／地图／平台 PCD，降级前拒绝存在零字节清单。

真实 ASGI HTTP→独立 Celery→MinIO 已完成纯 NuScenes、缓存复用、KITTI 混合三次成功交付，以及同长度 raw 与地图篡改的两次明确失败。双 Scene、双相机数据的十三条媒体逐字节核对，三次生成下载器实际运行均通过，包含空原始点云；幂等重导未增加 Creation 或替换 Item 来源。该资格不代表独立 TCP API 服务、浏览器或 GPU 性能验收。

用户／API／架构／恢复说明及 CHANGELOG 已更新，OpenAPI 无漂移、codegen、docs build 和代码格式检查已通过。完整回归、包含真实空辅助 Creation 的最终离线恢复、主树合入、人工环境 0236 刷新及按归属清理仍待完成，本检查点不作为 G2 Outcome。完整 E02／B13 和用途切换仍继续执行。

## E02-B13-B3-G2 Outcome — NuScenes 辅助原件与完整交付

主树已合入至 `5de7ba1b4`；独立归属与迁移为 `b65486ac3`，导入器为 `f888fef9c`、`a7289adfd`，冻结媒体与下载为 `5cb59fe3a`、`96455139e`，空 raw 兼容为 `131d5ad38`，恢复空对象夹具为 `bb0cf52f8`。0235 固定 raw／map 辅助归属及业务指针，0236 精确放宽交付清单零字节下界；图片／地图／平台 PCD 仍要求非空。原始五通道点云保持原字节，不用平台 PCD 替代。新导入和明确重导沿受控创建与完整核验，已登记幂等重导不替换原件；缺归属历史不能从当前桶猜测接管。

纯 NuScenes 与混合格式均使用 schema 2 冻结完整 Scene、原始路径、全部相机、raw 与地图，创建独立交付副本并沿准确原请求及当前关系复核后发布。共享地图核对每个引用 Task。副本、ZIP、Job 与缓存共用创建起七天截止，缓存复用不延期，旧 G1 责任继续可核验、退休和恢复。辅助原件在业务删除后保留 owner／Creation，不宣称自动物理回收或完整用途切换。

最终 27 个相关后端测试文件 1080 项通过，无失败或跳过；恢复工具三个相关文件的 71 项 unittest 通过。正常提交 hooks、Ruff／format、OpenAPI 无漂移、codegen 与文档生成检查、docs build 及 diff 检查通过；未运行远端 CI。本片没有前端源修改，不将 ASGI 或离线证据表述为浏览器／GPU 资格。

真实导入器→ASGI HTTP→独立 Celery→MinIO 完成五个 Job：纯 NuScenes、保持首次截止的缓存命中、KITTI 混合成功；同长度 raw 和地图篡改均明确失败且无输出。双 Scene／双相机共四个 raw owner（一个零字节）和一个共享地图 owner，十三条交付媒体逐字节核对 SHA／大小，三次生成下载脚本经公共网关实际运行成功。重导保持 Item 身份、原件指针和 Creation 数量，物理许可最终归零。

最终真实离线演练在 0236 先销毁源，再恢复 166 表／188 行及九桶 77 对象（13883 字节）。四个辅助对象完整核验，包含真实零字节未绑定 Creation；共享地图、业务删除后归属、未完成创建和受保护历史保持。旧 bundle／ZIP 内容、冻结事实及期限不变，含历史链接的准确缓存失效，不宣称旧 URL 已迁移或生产 RPO／RTO。首轮在源销毁步骤中断，未计入通过；按准确 owner 重试清理并独立确认空资源，最终串行演练通过。全部随机恢复环境已销毁并确认。

人工监督服务更新到本 checkout 和 0236，两 Worker 各 73 项注册、职责队列及实际空输入派发通过，绑定 Beat active，3100／8100 为 200、doctor healthy。12 表 155 行完整指纹、24 对象完整 SHA 和 12 源文件与本片前完全相同，新增辅助 owner／指针均为空。未重建镜像、安装依赖、改共享配置或分配发布版本。

test 的操作账本归零后，准确命名数据库、Redis 和九桶已销毁并由 doctor 独立确认不存在。四棵干净隔离树均证明提交已合入或 patch 等价后移除，任务分支及本片报告、下载、源夹具、构建与测试中间产物按归属清理；保留人工环境、虚拟环境、共享依赖、生成类型与工作树身份。正式资料位于用户导出格式／批次指南、API 导出指南、存储架构和恢复部署说明，README 与 CHANGELOG Unreleased 同步。普通图片／视频导出和其余 ML 原件消费者仍待继续，完整 E02／B13 与整套 epic 不归档。

## E02-B13-B3-H1 Design — 图片导出冻结输入与独立媒体交付

基线 `e1bdb34ed` 的只读盘点确认：普通托管图片异步 ZIP 仍按 Task.file_path 直签当前 datasets 桶，独立 Task 也可能猜错桶；缓存只有部分标注／选项指纹，下载器跳过已有文件且不核对真实 SHA／大小。已有引用图片严格读取并嵌包、ZIP 独立责任及 G1／G2 媒体物理链均复用。本片覆盖项目、批次和 Data Manager 选中任务的全部异步 image 格式，包括纯引用、托管／引用混合及空项目；同步 VOC、视频导出和 ML 输入留作独立切片。

新 image_media 证明使用 schema 3，私有输入保存在现有 ExportMediaBundle；零件清单已由当前数据库和物理层允许，不为纯引用或空项目新增平行 Job／owner。保留 schema 1／2 LiDAR 历史责任与现行请求合同；旧未完成图片 Job 缺少新证明时明确失败并要求重导，不在 Worker 重捕获今天的输入。HTTP 在同一短事务固定 Project、精确选中范围／Batch、顺序化 Task、canonical 原件及全部格式所需事实，不读取原件对象，不签原件 URL。

冻结保留现有格式、顺序、类别／工具／属性过滤、真实或回退尺寸、AAP 预测双数组、相关 SceneTrack／Interval、持久 RLE 引用及多目标目录合同。Worker 构建仅访问脱离 ORM 的冻结视图；RLE 继续由已有独立 owner 严格读取，不新开当前桶后门。预算先在 SQL 计量行数和 JSON 体积，再有界读取／分块冻结，JSONB 规范化后计算摘要。任务／来源变化、预测／标注／轨迹／类别变化和消息范围或选项改变都不能发布半轮结果。

托管图片统一 original_sources.resolve_task，覆盖已登记 Dataset 原件和 TaskOriginal；未知历史拒绝，不按路径、metadata 或当前桶猜测。每个 Task 保留准确绑定，副本仅按完整已登记来源身份去重；共享来源仍逐绑定复核。沿 G1 原件两槽、条件读取、完整 SHA／大小、独占写入、全量回读和真实关闭，复制原字节，不解码转换。独立副本登记在任何 PUT 前，准确 UUID 对象清单与 ZIP 配对发布；新图片角色不得被当成 LiDAR 相机或主件。

引用图片保留现有 tasks:read 原 Key 证明、节点能力和严格快照，原字节继续嵌 ZIP，不复制到交付桶；同一冻结视图负责其格式输入。每次原件 I/O 与最终事务重核准确引用绑定和当前权限。含任何引用的任务不跨 Job 采纳缓存，也不能只核对旧 ZIP 就声称当前引用仍可用；纯托管缓存纳入完整冻结输入摘要，核验保存位置的 ZIP 和每件媒体。空范围保持合法元数据 ZIP。

项目／批次继续既有 export.annotations 能力，不增加逐题可见性过滤；选中任务保持准确选择与逐题可见性，不能扩张为整项目。托管原件不新增 tasks:read／annotations:read；引用的已有额外权限独立保留。幂等重发不更新原证明、原 Key 或固定截止。最终沿 Job／账号／项目权限及 Key，再按 Project、Batch→排序 Task、Dataset→Item 和相关标注／预测／轨迹父行作短 NOWAIT 复核；父行锁封住新增成员，不能仅锁已存在历史。跨序竞争拒绝本轮，不带业务锁进行物理 I/O。

沿既有物理合同新增图片导出的明确上限：总 Task 最多 20000（含引用），托管单件 256MiB、全部图片原字节累计 20GiB、冻结 JSON256MiB、相关标注与预测各最多100000；其余关联集合同样先计量并有界。引用继续原64MiB／3200万像素限制并计入累计字节。该20k范围是新交付预算，不称为旧 mask adapter 的既有100k上限。8GiB临时空间、512MiB ZIP成员与200000 entries继续执行，不宣称实测 RSS／吞吐资格。

保留根级元数据、多目标子目录及 YOLO 的 images／labels 平行目录合同。新托管图片使用含 Task UUID 的稳定安全逻辑路径，引用保持其现有逻辑路径；COCO 文件名、AAP 匹配、YOLO 标签与三种 mask 清单统一使用同一映射，修正旧格式之间路径不一致，正式指南说明新布局。对路径穿越、不安全值及图片／标签／mask／生成元数据重名在入队前明确拒绝，不静默覆盖 ZIP 成员。清单继续现有字段并增加保存副本的真实 SHA／大小和共同截止。fetch_images.py 完整校验已有文件与下载内容，临时文件核验成功后原子落盘，安全处理站内相对 URL 和显式 base origin；嵌入图片在托管链接到期后仍可独立核验。副本、ZIP、Job 和缓存共同使用 bundle 创建起七天截止，不因命中、业务删除或恢复续期。

退役和清理继续 G1 的已发布 latch、精确保存清单、未知操作保护及可重试责任；恢复只映射当前 bucket／storage_identity，冻结业务事实、清单、历史 URL、期限和原件证据不变。含旧媒体链接的缓存准确失效，新交付须新责任。纯引用／空清单也验证 ZIP 配对、历史保护与删除，不添加裸扫或原件 GC。

超过八文件，按独立隔离树拆分：图片输入／路径／Job证明；冻结格式视图／图片交付／packaging及下载器；schema3 bundle领域／历史兼容与恢复测试。主树负责共享 Worker 生命周期适配、三类 HTTP 入口、集成回归、正式文档和真实验收。适配器显式携带图片或 LiDAR 合同，不用进程全局切换或复制整套发布／回收实现；现有 LiDAR 入口及回执保持回归覆盖。各修改代理基于本地 Design HEAD，真实提交后由主树合入。

验证覆盖九类图片 target／别名、纯托管／引用／混合／空范围、TaskOriginal与primary关系、同键异桶、同长度篡改、冻结输入和Key变化、消息改写、重复投递、取消与soft limit真实收尾、缓存／固定截止／回收、路径冲突、错误已有下载文件及恢复。用新建可销毁 test／e2e、实际 HTTP／Celery／MinIO 和生成下载脚本；无前端源改变不重复视觉矩阵。保留3100／8100、两 Worker／绑定 Beat及12表／24对象／12源文件指纹，不安装依赖、重建镜像、改共享配置或分配版本。全部验证和按归属清理后记 Outcome，不把本片视为 E02／B13 或整套 epic 完成。

### H1 implementation checkpoint — frozen inputs and shared lifecycle

隔离集成已接入图片 schema3 的准确范围、原件与格式输入，包含主链接图片、引用和空清单；格式视图、独立副本和严格下载器复用已有物理层。三个 HTTP 入口在有界冻结后从同一事实捕获引用授权，避免预算前先加载整范围和连接配置。共享 Worker 按每次尝试显式选择合同，引用仍严格嵌入且不跨 Job 缓存；旧 schema1／2 保留兼容分支。Data Manager 派发回填和派发失败仅修改尚未分配的 pending 行，跳过已认领锁，防止重复请求的迟到响应覆盖获胜 Worker。

已验证输入／归属／退役164项通过；新格式、下载、派发竞争、授权与旧 LiDAR Worker 的集成125项中120项通过，五个旧引用导出夹具尚待按真实冻结／回执合同迁移。新退役查询曾出现 JSONB 与整数比较的实际数据库错误，已显式使用 JSONB 数字并由后续164项验证通过。当前仍待补齐 Worker／HTTP 全链、最终格式及Mask预算回归、真实 MinIO／Celery／下载与离线恢复、正式文档检查、人工环境刷新和按归属清理，不作为 H1 Outcome。

### H1 implementation checkpoint — image delivery and recovery acceptance

图片 schema3 已接入共享独立媒体 Worker、三类 HTTP 入口和全部异步图片格式；显式协议保持 LiDAR schema1／2 行为，不以全局开关切换。冻结输入、canonical 原件、统一 Task 路径、严格引用嵌包、完整副本／ZIP 核验、固定七天期限及配对发布已集成。迟到派发不覆盖认领身份，取消与提交回执不确定时保留准确终态和候选责任。AAP mask 累计400万游程／256MiB对象 JSON、Indexed PNG 每图255实例／400万游程已实施并同步正式指南。

最终按各用例最新结果核对，43个相关后端文件1326项通过，无剩余失败或跳过；恢复工具四个文件80项通过。较早大回归的旧模拟数据未声明媒体类型或缺已登记原件，已改用准确视频分支或真实引用登记；外层回滚夹具的五项业务测试明确借用业务会话，不冒充独立连接资格。原件／副本／授权与取消／未知提交断言保留，新增图片配对提交不确定的两项覆盖。

实际 ASGI HTTP→独立 Celery→MinIO 完成13个 Job：混合全部格式、托管 TaskOriginal／direct／primary、首次期限缓存、批次／选中范围、两次无跨Job缓存的纯引用、空项目及异桶登记原件共9次成功；同长度原件篡改、原Key撤销、标注变化和引用来源停用4次明确失败且无ready输出。12条媒体引用完整核对，13次生成下载脚本实际运行通过，包含修复已有错误文件；引用的原有tasks:read边界保持。独立Task原件来自annotations桶，另一已核验历史Dataset原件来自非默认桶，同键错误当前桶未被读取。操作许可归零，专用Worker已退出。资格限ASGI与实际S3网关，不宣称独立TCP API、浏览器或GPU性能。

真实离线恢复先销毁源，再在随机目标恢复166表196行、九桶83对象（31107字节），包括两套schema3 bundle、四个媒体副本及两个ZIP；ready与真实中断building责任、旧URL、冻结事实和共同期限保持，准确缓存失效。恢复fixture使用真实inline组件并明确未经过HTTP／Celery派发；不据此宣称生产RPO／RTO。随机环境清理已独立核验。OpenAPI无漂移、codegen、docs build、正常hooks与格式／diff检查通过。主树合入、人工环境源码刷新与指纹核对、test／e2e和隔离树清理尚待完成，暂不记H1 Outcome，完整E02／B13仍继续。

## E02-B13-B3-H1 Outcome — 图片冻结导出与独立媒体交付

主实现已合入 `01a9bf999`，图片输入／路径与Job为 `8bd08164e`，schema3领域为 `de1ff0241`，冻结格式／副本／下载为 `38631f457`、`9a074cf7d`，三入口与共享Worker为 `7f8e952b8`，取消与未知提交为 `05dd4ebf4`、`2a138dcd0`，恢复为 `9b1ae48f8`。全部异步图片格式、项目／批次／显式选中、纯托管／引用／混合和空项目共用冻结业务事实、准确请求证明与现有独立bundle／ZIP生命周期；无新增迁移，人工迁移头保持0236。

托管TaskOriginal、direct／primary Dataset原件沿canonical保存位置完整核验，生成精确副本，托管与引用使用一致安全逻辑路径；引用继续严格读取并嵌包，保留tasks:read、原Key及真实节点边界。含引用不跨Job缓存，纯托管命中完整核验ZIP及全部媒体并保持首次截止。副本、ZIP、缓存和Job统一七天期限；生成下载器核对嵌入、已有和下载字节。派发回执、取消、重复投递或提交确认丢失均不能覆盖胜出终态或丢弃未发布责任；旧无证明队列需重导，不猜当前原件或回填合同。

最终43个相关后端测试文件1326项不同用例均通过，无剩余失败或跳过，按各用例最新结果统计而非累加重跑次数。恢复工具四文件80项unittest通过。正常hooks、Ruff／format、OpenAPI无漂移、codegen、docs build及diff检查通过；未运行远端CI。本片没有前端源修改，不重复既有浏览器视觉矩阵或宣称GPU性能资格。五项外层回滚夹具测试仅借用业务会话，独立连接和真实物理链由实际Worker验收承担。测试误选曾触发默认annotation_test的0180首条DDL并因缺表失败；只读复核迁移头仍0179、相关新表不存在且无遗留失败事务，未对该库重试迁移、修复或清理，人工数据指纹保持。后续测试均使用验证归属的隔离库。

真实ASGI HTTP→独立Celery→MinIO完成13个Job，其中9次成功、4次按预期拒绝；覆盖全部图片格式及yolo别名、混合持久RLE、TaskOriginal／direct／primary、固定期限缓存、批次／选中范围、无跨Job缓存的纯引用、空项目和同键异桶。12条媒体引用完整核对SHA／大小，13次生成下载脚本实际运行通过，含已有错误文件修复。独立Task读取保存的annotations桶，历史已核验Dataset原件读取另一保存桶，当前datasets同键错误内容未被采用；同长度篡改、撤销原Key、标注改变及停用引用来源均无ready输出。专用Worker退出，操作账本归零；资格为ASGI及真实S3网关，不表述为独立TCP API或生产负载验收。

离线演练先销毁源，再恢复166表196行、九桶83对象（31107字节），两套图片bundle的四份副本与两个ZIP完整核验。ready及真实中断building责任、准确Task集合、冻结业务事实、历史URL和共同期限保持，schema3缓存索引失效；恢复后重新交付须创建新bundle／ZIP，未重签旧链接。夹具真实inline执行组件并明确不冒充HTTP／Celery派发。随机源／目标清理均独立确认，不授予生产RPO／RTO。

人工监督服务在确认idle后按原配置刷新，两个Worker各73项注册、职责队列和实际空输入派发通过，绑定Beat active；3100／8100为200、doctor healthy。12表155行、24对象完整SHA和12源文件与本片前完全一致。未重建镜像、安装依赖、改共享环境或分配版本。test／e2e账本及其他连接归零后，两个数据库、两个Redis和18桶销毁，并由doctor直接查询确认不存在。五棵干净隔离树及分支均证明已合入或patch等价后清理，本片私有探针、下载、报告和测试／构建中间产物按归属清理；人工环境、共享依赖、当前checkout虚拟环境和生成类型保留。

## 下一片范围 — 视频异步导出的原件、时间事实与交付副本

只读盘点确认视频仍直签当前datasets桶，下载脚本跳过已有文件且不核验字节；输入使用可变video metadata及协作／质量事实，缓存和最后检查未覆盖全部变化。下一片复用共享bundle／Worker生命周期，定稿视频原件与metadata generation、脱离ORM的时间／轨迹输入、唯一sequence路径及严格下载。KITTI与MOT／YOLO混合包还需修正0／1基编号和共用目录冲突，保持绝对源帧、范围及采样相位。

现视频派生链的原件上限为512MiB，独立Task上传／原视频读取允许5GiB；媒体副本物理／DDL上限仍为256MiB。须明确schema4的单件与展开预算，同时保持历史schema1／2／3边界。TaskOriginal视频缺少可复用的F metadata generation，须先解决可信元数据合同，不能从旧JSON猜测或把playback当原件。本范围尚未实现，也不代表完整E02／B13或用途切换完成；之后仍需剩余ML原件消费者、整项矩阵及独立生产／外部目标门禁。

## E02-B13-B3-H2 Design — 视频冻结导出与独立媒体交付

在 `cd638ccf2` 上复核后定稿；复用共享 MediaProtocol／ExportMediaBundle／run_owned／ExportOutput，不复制发布生命周期。覆盖全部十种现有异步视频格式、项目和批次范围、空项目、Dataset原件与独立TaskOriginal；显式选中入口的既有限制保持。原件按canonical关系与保存位置完整SHA／大小核验，始终交付原视频字节；同SHA不同归属不合并，完整canonical身份相同的物理源只复制一次，保留全部Task授权绑定。

### 可信元数据与时间事实

F的metadata generation、指针、slots与回收均属于DatasetItem，不能直接宣称覆盖独立Task。H2采用导出专属的确定性探测合同：在共享transfer持有的完整核验stage FD上，使用已有本地ffprobe能力提取fps、实际frame_count、width、height与duration；冻结探测合同修订，结果与原件SHA／大小共同成为本次交付的metadata receipt，写入完整核验的ZIP／manifest。所有原件沿同一探测规则，不读旧Item.metadata.video、不以最大标注帧猜源长度、不替换成playback，也不创建伪Dataset。元数据探测不修改F generation或业务Item。普通范围仍保持绝对源帧、闭区间与原点0的采样相位；HTTP只固定合法Task／Segment身份与范围，实际源边界在Worker探测后验证，超界不发布。

冻结Project采样／协作配置、原请求scope、全部相关Segment、原始标注与候选质检Run／pairs，以及AAP实际消费的Prediction、Scene、Track／Interval。质检freshness使用两侧Segment上全部active且未取消的原始标注，不能从class过滤／sanitize／裁剪后的导出标注反算；纯重放保留较新stale不遮蔽较早fresh accepted的规则，同时间按UUID确定次序，不刷新数据库status。输出先按Segment核心区裁剪，再合并same_track，最后按请求范围裁剪。父Task／Item写锁围住新插入，现存Segment／Annotation／Run及AAP关系按稳定顺序NOWAIT锁住恢复与接受；物理I/O不持有这些业务事务。末端完整重采摘要和当前权限，改变则拒绝ready／cache。

### 路径、编号与预算

每个Task使用稳定唯一的sequence与视频逻辑路径；MOT、KITTI、YOLO、COCO、DAVIS、YouTube-VOS、MOTS及manifest共用显式映射。同名视频不覆盖，生成成员冲突在逐帧分配前拒绝。KITTI dense0基与MOT／YOLO／COCO dense1基使用独立frame_outputs；单格式布局尽量保持。DAVIS0基五位、MOTS遵守请求base；YouTube使用canonical裁剪后exact mask帧，不替换为普通网格。

schema4只允许video角色，单件原件／副本上限5GiB、最多1000Task、去重后总原件20GiB；维持schema1／2／3各自既有限额。迁移0237扩展物理清单并新增明确的schema4约束，原记录不回填，存在schema4时拒绝不安全降级。保存位置、独立责任、首次七天共同期限、一次3600秒claim、完整回读、缓存、退休与恢复沿共享合同；下载器校验嵌入／已有／新下载字节并原子替换错误文件。

冻结JSON仍限256MiB，原始标注／预测各10万。探测源帧最多100万／视频，累计实际采样／exact帧最多100万，所有target的track×frame求值／all_frames展开累计最多1000万；分配列表或逐帧字典前计算。单轨关键帧／outside／顶点及插值放大另有限额；portable mask沿4百万runs／256MiB JSON，单mask4MiB、单帧4096边长／16777216像素，累计raster工作最多268435456像素，每个DAVIS／YouTube序列最多254对象。预计ZIP成员在展开前限20万，现单项512MiB／临时总量8GiB兜底保持；格式不需要的逐帧／像素预算不误算。取消检查穿过任务、帧、轨迹与mask循环；探测子进程／物理线程须真实收尾后才释放stage与许可。这些是本地资源边界，不是生产规模或性能资格。

### 实施与验收

隔离树分别负责冻结输入／纯时间重放、视频探测与路径／下载／格式打包、schema4领域／迁移／物理上限；主树负责共享Worker／HTTP集成、回归和真实验收、恢复工具与正式文档。新test／e2e命名空间经launcher验证归属，全部数据库测试由主树执行；不触碰默认annotation_test。重点覆盖TaskOriginal／Dataset、同键异桶与篡改、原Key／关系／分段／Run／标注变化、重复／取消／未知提交、预算前拒绝、同名源、多格式编号、YouTube稀疏帧、错误已有下载文件、固定期限缓存与源销毁后隔离恢复。保留3100／8100、两Worker／绑定Beat和既有12表155行／24对象／12源文件指纹，不重建镜像、安装依赖、改共享配置、分配版本或宣称外部部署资格。完整验证、人工环境刷新和精确清理后再记Outcome；完整E02／B13和整套Epic继续活动。

### H2 implementation checkpoint — shared delivery and live recovery

schema4 领域、迁移、物理传输、冻结输入与时间重放、真实计帧及十种格式已合入；质量 Run 查询进一步只加载消费字段，并保留同一 SQL 快照中的预算守卫。主树已接通项目／批次 HTTP、消息证明、共享 Worker／缓存／取消和独立交付副本；TaskOriginal 帧范围支持空 Item，Segment 身份与关系读取有界。0237 的 PL/pgSQL CASE 括号和 ORM 派生 schema 在 flush 后的同步读取问题已在真实隔离库复现并修正。

独立 Celery 使用实际 Dataset／TaskOriginal 两类条件创建的视频、持久 mask、同名源和伪旧元数据完成 ASGI HTTP 验收：十格式项目／批次、同请求缓存、TaskOriginal 帧范围、空批次及超界拒绝／派发前取消共七个 Job；五条媒体与十一次下载／抽帧脚本执行通过。恢复演练发现 video_json 仍输出源存储键，现已与 AAP JSON 共用交付路径；额外两次实际冷导出核对全量／范围的两种 JSON 路径和三条副本字节通过。合计九个 Job，七成功、一超界失败、一取消，未把组件替身视为真实链路。

新增质量查询 28 项、路径相关 81 项及此前领域／传输 76 项和集成 104 项均有本地通过记录。共享回归的 1237 项中 34 项仍需适配已改变的内部接口与 owned Worker 路径，正在逐项保留原有断言；不宣称最终回归通过。恢复视频构造检查及纯恢复脚本通过，实际演练先销毁源，再恢复 166 表 216 行、九桶 92 对象（68278 字节），核对 ready／真实中断 building、四条视频副本、两个 ZIP、探测证明、固定截止及历史 URL 不变、相应缓存失效；最终随机环境清理已独立确认。失败演练曾有一次退出时资源清理未完成，随后验证零连接、按原 owner 重试销毁，并确认源／目标资源均不存在后移除目录。

用户／API／架构／恢复说明及 README、CHANGELOG 已更新。最终共享回归、主树集成提交、人工环境 0237／Worker 刷新、业务指纹复核及 test／e2e／隔离树清理尚待完成，本节不作为 H2 Outcome。完整 E02／B13、其余 ML 原件消费者和整套 Epic 继续活动。

## E02-B13-B3-H2 Outcome — 视频冻结导出与独立媒体交付

主集成 `a72cc3328`，质量投影 `41aa18439`、恢复工具 `bb0c9f5b5`、正式文档 `ce1926707`、既有回归迁移 `9174d9cf3`；领域与冻结／格式实现见此前各提交。迁移 0237 已应用于本工作树人工环境。本片完成，完整 E02／B13 与整套 Epic 继续执行，完整用途切换关闭，不分配发布版本。

- 十种视频格式共用冻结输入、已登记 Dataset／TaskOriginal 原件、完整 SHA／大小核验、真实计帧、独立副本和共享发布责任。时间范围保持绝对源帧与原点采样相位，质检使用原始事实重放；主路径不读旧视频 JSON。单件 5GiB、最多 1000Task、去重原件 20GiB 与展开预算共同限制工作；resolver 工作额外限一亿、预计展开 JSON 限 256MiB。Task UUID 路径贯穿两种 JSON、manifest 和各格式文件，MOT／KITTI、DAVIS／YouTube、MOTS 编号各自明确。
- 最终相关后端 48 个文件、1258 个不同用例通过：按最新有效 JUnit 替换修改过的模块后去重，无失败、错误或跳过。既有 34 项失败均已定位并保留对应行为覆盖，五文件最终 139 项通过；未用伪媒体类型绕过新流程。恢复工具七文件 105 项纯测试通过。这里只报告相关本地回归，不宣称全仓或远程 CI。
- 实际 ASGI HTTP→独立 Celery→MinIO／ffprobe／ffmpeg 共九个 Job：七次成功、一实际超界拒绝、一派发前取消。覆盖两类 canonical 原件、同名视频、旧元数据陷阱、全部十格式、项目／批次、缓存、独立 Task 帧范围与空批次；八条媒体 SHA／大小核对，十三次下载／抽帧脚本执行，包括两次实际替换同长度损坏的已有文件。另读取真实 ZIP 核对两条视频的 YouTube 精确源帧 `[1,5]`、五位编号及 DAVIS 调色板像素／最近可见帧保持；与普通 `[0,2,4]` 网格分别验证。
- 实际离线恢复先销毁源，再恢复 166 表 216 行、九桶 92 对象（68278 字节）。两套 schema4 bundle 的四条视频副本、两个 ZIP 和四份探测证明通过；ready 与真实中断 building、冻结来源与输入、首次截止及历史 URL 保持，恰好一个 schema4 缓存索引失效。恢复不回源、不重新签名、不续租、不自动重放；它是 inline 组件的合成恢复资格，实际 HTTP／Celery 验收独立记录，不代表生产 RPO／RTO。
- 人工 3100／8100 保留，使用原监督服务应用 0237 并刷新两个 Worker／绑定 Beat；两 Worker 各注册 73 个任务，实际空输入派发通过且 active／reserved／scheduled 均为空，doctor healthy。原 12 表 155 行、24 对象、12 源文件全量指纹保持。没有前端源改动，未重复视觉矩阵；没有安装依赖或重建镜像。
- test／e2e 的物理操作账本与未终态 Job 均为零，两个数据库、两个 Redis、18 桶按 owner 销毁后从实际服务器重新确认不存在。十个来源提交证明 patch 等价后，七棵干净隔离树及任务分支移除；任务下载、夹具、日志／报告、构建／测试中间产物按归属清理。正式资料已同步用户导出格式、API 导出、存储架构和恢复部署说明，以及 README／CHANGELOG；OpenAPI 无漂移、codegen、docs build、Ruff 和正常 hooks 通过。

## 下一片范围 — 托管图片预标与失败重试的原件输入

只读复核确认图片预标主流水线、ROI 和两类失败重试仍通过 `storage.resolve_task_url`／`_load_task_image` 使用当前桶，混合引用作业的 managed 成员也回退旧读取；独立 TaskOriginal 与 primary-only 关系尚未消费 canonical locator。下一片先闭合现有图片 bulk／workbench 预标和失败重试，不重新建设已完成的逐帧视频原件读取，也不扩展新的点云 ML 功能。

实施前先定稿原请求／精确 Task 集合与配置／来源证明、同一完整核验 stage 的整图和 ROI、现有 PredictionInputLease 的固定保存位置／独立对象清单及真实收尾、末端来源／Key／批次分派复核，以及无可信来源历史重试的明确兼容策略。现有部分 ML backend 按去 query 的 URL 路径缓存 embedding，重新签同一路径不足以证明消费本次字节；应使用每次尝试独立的精确输入副本路径，保持后端协议并避免重建 ML 镜像。同步 interactive／predict-test、secondary、mask repair 和整段视频 Tracker 继续作为独立后续消费者，本段尚非实施或验收结果。

## E02-B13-B3-I Design — 图片预标与失败重试的固定输入

实施检查点：请求／Worker／固定原件与临时输入责任已合入，后续 SQL 类型修复 `874df35d9`、共享输入回归 `4e0d935d7`、导入桶过期规则修复 `4f16d70d6` 已提交。核心 420 项、owner 37 项、共享输入／请求与 lifecycle 118 项不同本地用例已有通过结果，恢复相关纯测试 44 项通过；先销毁源后恢复 166 表 218 行、九桶 95 对象通过，清理已核实。扩大后的旧 Worker／请求回归暴露缺少 canonical 来源和冻结证明的历史夹具，正在逐项迁移并保留原行为断言。真实 HTTP／独立 Celery／MinIO、本轮人工环境刷新及最终清理尚未完成，本段不构成 Outcome。

真实能力复核修订：当前 MinIO 对错误 ETag 的 DELETE 仍返回成功并删除对象，不能把 SDK 接受 `IfMatch` 当作服务端条件删除资格。本片改用非 null、不可变 S3 VersionId 约束临时输入：新写入在物理许可内确认目标桶版本控制已启用，PUT／完整回读／末次 HEAD 必须确认同一明确版本，签名 URL 固定该版本；物理清理只删除核验过的准确版本，`IfMatch` 仅作额外条件。版本控制未启用、并发暂停或返回 null 时拒绝新发布并保留 planned 责任；历史 null 回执不回填，继续允许退休／恢复映射，但不执行删除。应用不自动开启版本控制，真实验收只配置已核实归属的一次性桶，人工部署另核部署顺序；不重建镜像。

备份同步窄扩展为每 key 恰好一个当前版本、无历史额外版本及删除标记的内容快照，源读取固定观察版本，拒绝遗漏历史字节；不授予版本历史恢复资格。恢复保留原 inventory VersionId／写入标记／首次期限，只独立核验恢复内容，不能把目标新版本改写成历史回执或自动重放／清理。先前未版本化恢复结果仍保留其原资格，新版本化合同必须重新实际验收。0239 只收紧新发布回执，不能因旧 null 责任而阻断其状态推进。以上合同在代码、回归、真实 HTTP／Worker／MinIO 和源销毁后恢复通过前继续 implementing。

本片闭合现有 image 项目的 bulk／workbench 后台预标、整图／ROI 多阶段和单条／整 Job 两种失败重试。沿用 composer、管线验证、队列、原生掩码写入与普通预测采纳；不增加 ML 后端协议，不重建镜像。同步交互、secondary、mask repair 的托管来源及整段视频 Tracker 仍独立后续实施，已有引用入口的输入副本共享物理实现必须保持可用。

所有新图片请求在入队前冻结过滤后的准确 Task 集合、执行配置和 schema2 服务端来源证明，包含原请求 JWT 的期限／generation／jti 或 API Key 的准确身份／权限／期限证明。沿用实际入口所需 scope，不为托管请求额外要求 tasks:read；引用仍保留既有读取 scope。来源集合独立于已有 1MiB 配置快照，最多 1000 Task、序列化证明最多 1MiB；skip_predicted 先过滤，空集合保持空集合。幂等 pending 重投复用原选择、快照和来源，不重新捕获；历史未记录托管来源的图片消息不得在 Worker 补造证明，明确要求重新发起。

沿现有 ReferencePredictionJob 扩展混合输入编排，不增加竞争 Job owner。托管成员通过 original_sources 冻结 TaskOriginal、直接 Item 或 primary-only 关系，以及原件内容、保存位置、配置和维护代次；来源摘要覆盖完整 canonical 事实和准确关系，不猜测当前用途桶。Worker 在物理读取前后、每次 ML 派发前和最终保存前复核原请求、项目／Task 范围与相同来源。原件条件读取、完整 SHA／长度、64MiB／3200万像素检查后，整图和全部 ROI 共用同一份 Snapshot。物理许可覆盖读取、文件真实关闭与异常收尾；软限时、取消和任意 BaseException 均排空实际线程／Body／client，未知关闭保留耐久责任。

直接扩展无业务级联 FK 的 PredictionInputLease：新合同 version2 保存固定 import storage_ref 和只追加的精确相对 key 清单；对象先独立提交 planned 责任和随机写入标记，再条件创建、完整 GET／EOF／SHA／size、对象身份及 S3 lease／写入标记回读，最后单调发布 verified。同 SHA 不能代替独立归属证明。每次尝试使用新的 UUID 前缀及内容 SHA 路径，原图和所有裁剪共用一个 lease。保持单对象 64MiB、10000 对象／4GiB 累计、2小时活动租约、最长1小时且不对齐续期的 URL 和7天恢复观察期；已开始但未确认关闭的物理责任不因期限自动解除。移除平台创建的 import 整桶七天过期规则，保留并独立核对运维规则。ROI 先做有界纯裁剪，再在异步管线层逐项 await 上传，不在同步回调内隐藏事件循环或 DB 事务。

清理复用现有维护任务，按保存位置、准确对象清单和版本执行，未知 key／版本或任意位置上同 purpose 的未收尾 StorageOperation 均阻断删除。旧无 pin／清单 lease 保留事实并返回 legacy_unpinned，不按当前配置回填或盲删前缀。迁移约束固定身份／期限、追加清单及单调状态，downgrade 拒绝已有新合同责任；离线恢复只映射操作位置，不改变原来源证明、对象事实、首次期限或恢复执行。

最终写入在原 Job→Actor／Project／Key→Batch／Task 锁规则下，先复核准确来源与配置并取得不可伪造的事务绑定 receipt，再执行 overwrite 删除和整题预测／计数提交。引用原生掩码资格保持，托管 receipt 不借引用资格或后端 JSON 冒充来源；受保护 original_input 元信息只由服务端写入。保留 root 按 parent_stage is None 判断、非零 root、多层 ROI 谱系和 keep_parent／drop_box 语义，撤权／来源变化不能降格为可提交部分成功。bulk 的未分配 draft 保持 draft，工作台锁和终态规则不放宽。

完全失败和部分失败均保存冻结的原来源绑定。两种重试入口重新取得当前请求授权并冻结当前 Task 版本，但复用原配置和原图片内容／来源，不能将旧 Task version 与新授权混为一谈。缺可信来源或配置的历史托管图片失败保留记录并明确拒绝直接重试，用户可重新发起新预标；迟到 Worker 消息亦执行相同拒绝。部分重试保留旧候选，成功仅替换准确旧候选。

实施在本地 Design HEAD 的隔离工作树；根工作树独占新建可销毁 test／e2e 数据库。验证真实登记的 TaskOriginal／DatasetAsset／primary-only、当前桶陷阱、整图与 crop 实际像素、同键变化、撤权／取消／软限时、非零 root／深层谱系、覆写事务、幂等和两个重试入口；覆盖共享引用／缓存／修复回归，以及真实 HTTP／独立 Celery／MinIO 和源销毁后的隔离恢复。同步正式用户／API／架构文档和 CHANGELOG，刷新人工 Worker 并保持3100／8100、原业务和字节指纹，按归属清理测试产物。整套 Epic、完整 E02／B13 和完整用途切换仍未完成。

## E02-B13-B3-I Outcome — 图片预标与失败重试固定输入

2026-10-25 完成本地验收。请求与原件证明主实现 `36bfe4f30`、临时输入独立归属 `5a585bedf`、版本固定及迁移 0239 `af90a0f36`、备份恢复 `e2aff0c26`、取消边界 `ce1a2126e`、版本化暂存兼容 `dfd45919d`、真实流水线验收 `f942dfa7c` 已合入。完整 E02／B13 和整套 Epic 继续活动，完整用途切换关闭，不分配发布版本。

图片 bulk／workbench 预标在请求时冻结准确 Task 集合、配置、原始凭据及 canonical 原件关系；覆盖 TaskOriginal、直接 DatasetItem 和 primary-only 关联。整图与 ROI 共用一次完整核验的图片，目标副本使用每次尝试独立路径、随机写入标记及追加清单。单条失败和整 Job 失败重试保留原配置及来源，重新核验当前权限和任务版本；缺可信证明的历史失败明确要求重新预标。结果与覆盖清理、计数和来源回执同事务，接单阶段及提交前取消不丢失取消语义。

真实 MinIO 已证明 DELETE 的 IfMatch 不能独立提供所有权条件，因此新输入要求导入桶启用版本控制，PUT、完整回读、签名 GET 与删除均固定同一非 null VersionId。历史 null 责任不回填、不盲删；未知对象／版本／删除标记或未收尾物理操作阻断回收。另一实际兼容问题是版本化暂存的分段复制不回显源版本：仅字段缺失时先确认中止准确 MPU，再沿冻结版本流式读取并完整核对内容，继承原截止预算后条件封存；显式版本错误仍拒绝。实际探针验证删除旧版本保留新版本及字节，探针和 MPU 全部清理。

最终汇总留存 JUnit 并排除三个已更名的旧用例，938 个当前不同 pytest 用例全部通过；其中最终核心六文件 157 项、暂存 writer／独立创建账本四文件 121 项通过，无失败、错误或跳过。新版备份／预测输入恢复辅助／映射的 43 项纯测试通过。包含取消真实收尾、原 Key／来源变化、未知关闭、写入时版本控制暂停、PUT 应答后当前版本变化、隐藏历史及删除标记、完整内容／预算、固定与共享引用输入、请求／Worker／两种重试和事务提交边界。

真实 ASGI HTTP（无认证或数据库依赖替换）→独立 Celery CPU Worker→MinIO 完成八个 Job：三来源批量、工作台、根失败后单项重试、裁剪部分失败后 Job 重试，以及真实读取后的撤权和绑定变化拒绝。受控 HTTP 后端实际下载签名整图和裁剪，核对 VersionId、SHA／字节数及像素；7→9→11 的非零根链核对已交付子几何的属性及父子谱系。十个本轮输入账本完成精确清理，17 个已核验版本删除后均不可读取，无剩余临时对象／版本及 StorageOperation；维护任务实际派发一次，Worker 空闲后优雅退出。此前中断验收的三个账本亦由同一维护任务完成，整个 test 模式最终十三条均为 deleted。此证据资格化管线与存储协议，受控 CPU 响应不代表真实模型精度、GPU 或生产性能资格。

新版离线演练先销毁源，再在随机空目标恢复 166 表 218 行、九桶 95 对象、69115 字节。两个真实 v2 输入责任保留三个 verified 和一个 planned 成员，目标当前内容独立验证；三条旧版本读取回执及两次旧清理均明确拒绝，原 inventory／来源证明／首次期限不改写，不恢复队列。备份只资格化每键一个当前版本的内容恢复，不包含版本历史恢复或生产 RPO／RTO。随机源／目标及故障准备环境均核对清理。

人工 3100／8100 已应用 0239，准确所属的本地 dev 导入桶已验证开启版本控制；两类新 Worker 各注册 73 项任务，CPU 与 maintenance 实际空派发通过，绑定 Beat 保持运行。原十二表 155 行、24 对象完整字节和十二源文件指纹完全一致，HTTP 两入口返回 200。test 数据库、Redis、九桶及 e2e 资源由 doctor 独立确认不存在；十四棵隔离树在干净、提交已合入或 patch 等价且无所属资源后移除。任务日志、下载、缓存与构建产物按归属清理，保留人工环境、共享依赖、本地虚拟环境、生成类型及工作树身份。

README、CHANGELOG Unreleased、用户预标指南、预测 API／架构、存储架构和版本化部署／恢复说明已同步；OpenAPI snapshot 无漂移、codegen、docs build、Ruff／格式、正常 hooks 与 diff 检查通过。下一片先定稿托管图片的同步 interactive／predict-test 及其原生 Mask 候选最小来源绑定；secondary、mask repair、整段视频 Tracker、真实训练消费回执与生产回收继续独立推进，不归档本活动 Epic。

## E02-B13-B3-J Design — 托管图片同步推理与原生候选同源接受

2026-10-25 基于干净 `d6f4e158a` 核对真实调用后实施。本片覆盖 `predict-test`、`interactive-annotating` 的托管图片供图、原生 Mask 候选／logits 身份及 create／refine／重放；保留引用图片与视频现有合同。不增加 AsyncJob、表或 ML 后端协议，不重建镜像、不安装依赖、不修改共享配置。secondary、mask repair 和整段 Tracker 留待后续完整切片。

新增托管同步操作适配器，由现同步上下文管理器分派；业务权限复用现管理者测试和可编辑交互规则，不暗增托管入口此前没有的 tasks:read／annotations:write scope。带原标注提示仍要求 annotations:read；完成任务的只读 predict-test 不受交互开关限制。冻结原 JWT／API Key、Task.version、批次与 canonical 原件完整事实；在短事务提交后分配 PredictionInputs v2，以同一 input UUID purpose 持有来源／目标许可，完整核验一次原图、固定真实尺寸，再上传非 null 版本的尝试独立副本。setup 后及模型派发前重新核权；最终账户→项目／成员／原 Key→编辑 advisory→Batch／fresh Task→canonical 关系／来源／Gate→提示 Annotation→input lease／Gate，沿调用者顶层事务核验。最终不能另开 session guard、提交事务或执行对象／模型 I/O；凭据复核保留现授权缓存检查。初始成员锁必须在读取原 Mask 提示前提交，不能等到适配器内才释放。

候选与 logits 共同签入独立 `original_input` 小投影：schema=1、task_id、owner_kind、content_id、SHA、size_bytes、实际 width／height、location_id／location_revision／maintenance_revision 和 facts_digest。严格限定字段和类型，并与 reference_input 互斥；不包含私有 endpoint／bucket／key／完整 facts／凭据或签名 URL，不把托管来源伪装成 reference。身份不含 Task.version，避免正常标注编辑使同源候选失效；当前请求及最终边界仍核对冻结 Task.version。旧托管候选／logits／Decision 缺可信来源时不补默认值，要求重新推理。继续保留 routing／model／variants／Task／frame／prompt source 精确匹配、exemplar both 原候选下标，以及短候选过期后的合法 Decision 精确重放；来源变化时重放仍拒绝。

原生接受新增独立私有 native-original receipt，绑定真实 issuer、同 session／顶层事务、actor／Task、请求摘要、单个 Mask 摘要、完整来源及实际尺寸。资格化先在无业务锁阶段重新完整读取原件并确认关闭，随后取得最终锁和复核，不能在源句柄关闭时持写入锁。接受不创建无需交给模型的临时副本。PredictionService 增独立 native-original 分支，与其他 receipt 互斥且仅准 interactive_accept，证据写 original_input。ai-mask 首验、Prediction 和 Annotation 的全部图片尺寸复验显式传递同一受信任 receipt，不能从候选 RLE、Task JSON 或普通尺寸字典获取可信大小；视频与引用既有校验保留。Decision 内部来源证据只供重放，不进入公开响应。TaskOriginal 没有尺寸字段，primary-only 也不回退直接 Item 猜测。

所有取消／软限时保留实际 Future 并等待线程、Body、客户端和临时文件收尾；首次未知关闭标记粘住，不能用第二次 close 的 no-op 或 TTL 证明结束。退出先释放调用者事务，再关闭目标输入及来源；同 purpose 导致首次回收延后一轮属于正常保护。独立原件无业务级联，测试使用所属隔离 schema／namespace 清理不可变事实，不能删除账本代替真实清理。

操作层、native 接受／身份层和回归夹具分别在当前 Design HEAD 的隔离树实施；主树负责 endpoint 接线、正式文档、全部数据库测试及真实验收。覆盖三类 canonical 来源、同键异桶和同 SHA 新归属、原 Key／JWT／角色／批次／编辑锁／Gate／提示变化、setup 与真实读取后的拒绝、非方形尺寸、候选与 logits 旧／缺失／双来源、create／refine／If-Match／精确重放、伪造和跨事务 receipt、提交失败无半题结果、重复取消及未知关闭。真实 HTTP／MinIO 验收需实际 GET 签名版本、校验像素并完成接受／续点和延后一轮精确清理；纯受控输出不代表真实模型或 GPU 资格。同步 API／用户／架构／CHANGELOG，保留人工3100／8100、0239／两 Worker／Beat及十二表155行／24对象／十二文件指纹，完成相关验证和按归属清理后才记 Outcome。

### J 实施检查点 — 同步端点与原生接受

操作层、私有 native receipt、原件来源身份与独立提交夹具已合入；主树接通两个同步入口，先释放初始业务锁，再冻结提示元数据、核验原图及真实尺寸、读取原 Mask，并分别签入 original_input／reference_input。原生接受沿同一私有 receipt 贯穿 Prediction／Annotation 与尺寸校验；Mask 写入后及提交前复核原凭据时钟、Key 与 JWT 注销缓存，保留旧 Mask 错误字段及 scope 语义。

当前一次性环境的重点回归 72 项全部通过，覆盖三来源、实际输入版本、两端点、来源与凭据变化、原生 create／refine／过期候选精确重放、真实事务私有凭据边界及写入期间过期／JWT 注销的整体回滚；提示／会话／代理 67 项检查通过。夹具已修正编号长度、分区派生外键、触发器写入范围及请求事务生命周期，未放宽生产约束。真实 ASGI／HTTP／MinIO 验收、引用及共享服务最终回归、人工环境复核和精确清理仍在进行，本节不作为 J Outcome，完整 E02／B13 与整套 Epic 继续活动。

## E02-B13-B3-J Outcome — 同步图片推理与原生候选同源接受

2026-10-25 完成本地验收。同步原件适配器 `6fa951f56`、私有原生接受 receipt `49e72fdef`、主接线 `752680b2e`、实际选池后的派发前复核 `d89e63674`、真实验收脚本 `878a61ee2` 及来源失效恢复提示 `2e0ea741a` 已合入。迁移头保持 0239，无新增表、AsyncJob 或 ML 协议，不重建镜像。

predict-test／interactive-annotating 覆盖 TaskOriginal、直接 DatasetItem 和 primary-only 三种 canonical 原件，完整核验一次后使用尝试独立、非 null VersionId 的临时副本。候选与 logits 共同绑定严格的 original_input 安全投影；正常标注编辑不改变原件身份，缺可信来源的历史候选不能补默认值。setup、实际选池后的模型调用前及最终发布分别复核原凭据、Task／Batch、来源与 Gate；最终锁内没有对象或模型 I/O。保留原管理者测试权限、交互开关及 Mask 提示读取 scope，不扩大其他入口权限。

原生 create／refine／精确重放复用同 session／顶层事务的不可伪造私有 receipt，贯穿 Prediction、Annotation 与真实图片尺寸校验；先完整读原件并确认关闭，再取得最终业务锁。Mask 写入期间 API Key 过期或 JWT 注销均整体回滚。原 Key、来源、提示版本或 Task 变化拒绝在途结果；来源变化及历史候选缺证据时向用户明确提示刷新页面后重新推理。Decision 的内部来源事实不进入公开响应。

保留各轮 JUnit，按 classname／参数化用例名称合并最终结果，395 个不同 pytest 用例通过，无当前失败、错误或跳过；不计已删除的 predict-test/setup 组合，因为该入口本来没有 setup。最终 77 项包含真实引用 Mask 修复及 native 回归；另已通过真实引用缓存后台预测，共享提示／会话／代理、路由、RLE writer、Pipeline、幂等与引用同步／原生链路均通过。早期夹具问题已修正隔离 schema、分区派生外键、触发器表范围和请求事务；清理断言承认已删除输入的耐久 tombstone，物理版本仍由真实 HEAD／清单另行验证，未放宽生产约束或改写清理期限。

真实 ASGI HTTP（无鉴权或 DB 依赖替换）→受控 CPU HTTP 后端→MinIO 的最终验收覆盖三来源的两个端点、两种来源的原生 create／refine／重放与 logits 续点，实际下载固定版本并核对 SHA、字节、非方形 128×96 像素。真实 GET 后撤 Key 返回 403、改 canonical 绑定返回 409。最终运行的十二个输入与十二次签名 GET 对应，等待真实 next_cleanup_at 并分轮清理后无临时对象、版本、删除标记或未收尾操作，原件仍在。两轮脚本和旧链路的整个 test 模式最终二十八条输入均为 deleted，无活动 Job、其他 DB 客户端或 StorageOperation。受控输出不授予真实模型精度、GPU、独立 TCP API、浏览器或生产性能资格；本片无新 schema，不重复宣称新的备份恢复资格。

人工 3100／8100、两 Worker／绑定 Beat 均健康，十二表 155 行、24 对象完整字节和十二源文件指纹完全一致。新增用途仅由同步 API 调用；Worker 默认路由调用及任务合同保持，未触发镜像重建。test 数据库、专属 Redis 和九桶由所属 launcher 销毁，独立 doctor 实际核对均不存在；四棵干净且 patch 等价的隔离树已移除。任务日志、JUnit、缓存及文档构建产物按归属清理，保留人工环境、虚拟环境、共享依赖与生成类型。

README、CHANGELOG Unreleased、SAM 用户指南、ML／任务 API 和预测架构已同步；OpenAPI 无漂移、codegen、docs build、Ruff／格式、正常 hooks 与 diff 检查通过。下一片先推进托管图片 secondary 二次推理的原件、独立 crop 与父标注最终事务；Mask repair、视频 Tracker、E10 真实训练消费和生产回收仍为独立完整切片。完整 E02／B13 与整套 Epic 未完成，完整用途切换关闭。

## E02-B13-B3-K Design — 托管图片二次推理与独立裁剪责任

2026-10-25 基于 J 收尾 `035353d14` 核对 secondary-inference 路由、服务、ROI 与输入账本。当前图片分支仍按旧路径读原图、同一 Annotation 裸 PUT 固定 crop key，远程推理后使用旧 ORM 对象写回。本片将选中框二次推理完整接入 canonical 原件、每请求独立输入责任与父标注最终事务；保留属性人工值保护、AI model_ref、子框一层谱系、坐标回映、32px 下限和既有 400／422／502／504 语义，不新增 Prediction 或异步 Job。引用图片仍由现有入口明确拒绝，视频既有行为不借本片获得新资格。

沿 J 原件适配器建立真实 secondary_inference 操作：同样冻结原 JWT／Key、Task／Batch 和三类 canonical 绑定，额外冻结父 Annotation 的版本、几何、类别、属性及 meta、工具／父子／视频归属和活动／取消／锁定事实。重用严格 annotation.write 判据，审核员不开放 review-adjustment，不增加 ai_interactive_enabled 要求，API Key 始终保持 annotations:write。当前原件完整核验一次，用 Snapshot 的真实尺寸裁切，deferred ROI 编码在线程中完成并在取消时等待实际收尾；只上传裁剪，不上传无用途整图。每请求随机 UUID，经 PredictionInputs 的条件创建、完整回读、非 null VersionId 和准确 GET／删除供图，同一 Annotation 的并发请求不能共用对象。

窄增迁移 0240，在模型和服务的 owner-kind 合同中加入 secondary_inference，沿用 0238／0239 账本结构、不可变事实、期限和维护清理。降级遇到已有新用途责任明确拒绝，不能删除 tombstone 或改 owner_kind。离线恢复仍映射运行位置，不改来源／库存／首次期限；核对现有恢复验证器是否有封闭用途集合。

实际选池后的 before_dispatch 再核权并提交短事务，随后才能模型 I/O。最终沿账户→项目／成员／原 Key→Task 编辑 advisory→Batch／fresh Task→canonical 来源／Gate→父 Annotation 写锁→输入元信息顺序复核。父标注任一冻结事实变化均拒绝并提示重试，不能静默以旧属性覆盖人工编辑。最终当前 Task／Annotation 用于全部属性或子框写入、actor 证据、Audit，写入后再核原凭据时钟／JWT 注销缓存，沿同一顶层事务整体 commit；不在对象读取／模型调用／源关闭时持业务写锁。退出仍先 rollback 释放调用者事务，再真实关闭输入和来源；未知关闭保护不可由 TTL 或二次 no-op 解除。

操作与窄迁移、服务／路由、回归夹具分别在当前本地 HEAD 的独立工作树实施，根树独占可销毁数据库及实际验收。覆盖三类来源、同键异桶陷阱、非方形真实 ROI 像素／固定版本、同标注并发输入、人工属性保护／AI 更新／未知类别回退／一层父子、无检出和错误映射，以及来源／Key／JWT／角色／批次／编辑锁／父标注在读取、真实派发与写入边界变化时零半套结果。验证取消真实收尾及 unknown-close 责任，执行旧 secondary／权限、共享原件／输入／引用与 Pipeline 回归及真实 HTTP／MinIO。同步用户／API／架构／CHANGELOG；迁移和运行刷新只作用于本工作树所属环境，保持 3100／8100、两 Worker／Beat 和原业务／字节指纹，精确清理后才记录 K Outcome。完整 E02／B13、用途切换与整套 Epic 仍未完成。

## E02-B13-B3-L Design — 托管图片 Mask 修复的完整原件资格链

2026-10-25 基于 K 集成 `563f7fcbf` 对计划、Worker、变更、候选预览／采纳和回滚完成只读审计。本片完整覆盖托管图片 Mask repair 的 dry-run→execute／resume→确定性修复或 SAM 候选→内容预览／采纳→严格 rollback；保留引用图片与视频现合同，视频 Tracker 独立推进。现 managed 路径既缺原件／模型冻结，也沿当前桶 URL 和直接 Item 尺寸供图，候选采纳不经过引用专有资格链。不得仅替换 URL 后宣称完整交付。

计划为托管图片保存有版本的完整 canonical 原件／关系事实、计划时 Task binding、源 Annotation 的几何／版本／类别／工具／活动事实，SAM 另冻结池／模型声明摘要和准确配置；继续将完整私有计划纳入不可变 digest。模型身份只承诺已声明配置，不虚构权重摘要。execute／resume／rollback 记录本次真实请求的原 JWT／Key、准确 Job／Celery／batch／plan／actor 和当前任务执行版本；续做仅更换授权与 attempt，不重选原件或改写模型、计划来源与既有结果。合法的 Task 版本演进、已提交 shard 的自有版本递进及 COMMIT 应答丢失按现 checkpoint 规则处理，不能用永久计划旧 Task.version 拒绝正常续做，也不能将父标注变化无条件当作合法演进。

私有证明放现有 JSONB，不新增表或 owner migration；PredictionInputs 已允许 mask_repair。使用既有会从 Job DTO 隐藏的 repair_authority 键，若混合 reference／managed 需要新 envelope，显式分派并保留旧引用合同，不能默认整批都是一种来源。batch_out 现原样公开 plan_json，须先增加深拷贝的安全公开投影，隐藏完整 locator／facts／凭据／私有模型事实，保留公开计划、状态、digest 和必要的安全来源摘要；所有 dry-run／batch／Job／候选响应测试不得泄露 endpoint、bucket、key 或凭据。不得为脱敏改写已保存计划或其 digest。

新增专属 ManagedRepairUnit，复用 prediction_originals 的三 canonical 解析、原凭据复核、完整 Snapshot、真实尺寸及 physical／settled 收尾。SAM 用每 attempt／unit 独立的 mask_repair 输入责任、同 purpose 的源／目标许可、条件创建、完整回读和非 null VersionId URL；实际选池后核冻结模型声明与配置，再核当前授权／来源／父标注／输入，提交短事务后派发。确定性修复、回滚、预览和采纳不创建无用途模型副本。原件图片物理读取、解码与确切关闭在最终业务写锁之前完成；未确认关闭不得签发可提交资格，重复 close 或 TTL 不能解除未收尾责任。保留现 Snapshot、输入、RLE、action／shard 和 rollback 预算。

repair_original_receipt 为独立私有类型，绑定真实 issuer、同 session／顶层事务、actor／Task、来源／实际尺寸、准确 batch／plan／unit／attempt、操作种类和 payload／result digest。分别限定预测生成、确定性变更、回滚、候选读取及采纳资格，不能从普通尺寸字典、后端 JSON、ml_backend receipt 或 J interactive_accept receipt 获得权限。显式贯穿 PredictionService、MaskMutationService、AnnotationService 与 raster 尺寸校验，使 TaskOriginal 和 primary-only 真正覆盖生成、mask-content GET、采纳和回滚。收据在幂等返回之前仍核当前权限与原件，合法重放按已提交 operation/checkpoint 的前后版本核验，不能错误地要求已经由本操作修改的标注仍等于最初几何。

Worker 保留准确 attempt／取消围栏及 Job→batch 的锁骨架，再沿账户／项目／成员／原 Key→Task advisory→Batch／fresh Task→canonical／Gate→模型／RLE 内容→Annotation 顺序复核；最终来源检查只用调用者 session 的元信息，不另开 guard session。全部候选、Meta、candidate_records 的 result／metadata 摘要、operation、lineage、unit checkpoint 和进度在现有事务中统一提交；写入后核凭据期限及 JWT 注销缓存。原件图片／模型 I/O 不跨最终锁；现有已归属 RLE 的读取、写入及像素代数验证按既有变更事务保留，不以删除校验来假称整个 final 没有物理 I/O。

Worker 修复继续使用 review.write、任务阶段／有效执行者、编辑锁和 native gate；仅 SAM 检查交互 AI 开关，确定性修复在关闭 AI 时仍可执行。managed 请求保留 annotations:write Key scope，引用保持现有三 scope；候选内容读取沿既有读权限，采纳沿当前调用者的 annotation-write／phase／edit 权限，不额外要求 reviewer 或新的读取 scope。采纳复核准确计划项／模型、已提交 unit、候选 ID／index、result／metadata 摘要、源 Annotation 和当前 canonical 原图；并发接受只能建立一条标注。驳回不读取原图，来源离线仍可驳回，已驳回候选不得再接受。

历史 managed 计划、旧 delivery 或候选缺可信来源时明确拒绝执行／续做／回滚／采纳并提示重新预览建计划；不按当前桶或后端返回补造历史证明。历史查看、已提交结果和候选驳回继续可用。离线恢复只重映运行位置，完整计划／候选／输入来源和首次期限保持；旧证明无法核验时明确过期，不恢复执行队列或续授权。

按当前本地 HEAD 的独立树分工原件 owner／计划授权／公开投影、Worker／Mutation、候选预览／采纳及专属 receipt；根树集成、文档并独占测试与真实运行。三来源夹具须真实登记非方形原图与同尺寸 RLE，包含 current-bucket 陷阱，覆盖实际 dry-run／execute／独立 media 队列 Worker／SAM native／候选内容 GET／普通 accept、确定性精确像素／lineage／checkpoint／严格 rollback、失败续做与旧 delivery、来源／Key／JWT／角色／阶段／锁／模型／父事实竞争、跨 session／嵌套事务／伪造 receipt、取消／软限时／未知 close 和公开脱敏。引用 live 回归保留；真实 HTTP／MinIO 固定版本和准确清理不得用纯模拟结果替代，受控 CPU 输出不授予真实模型／GPU／生产资格。同步正式用户／API／架构／CHANGELOG，刷新实际受影响 Worker，保留人工 3100／8100、两 Worker／Beat 与原业务／字节指纹；验收和所属资源清理后才记 L Outcome，完整 E02／B13、用途切换及整套 Epic 继续活动。
