# EPIC-02：存储位置、连接配置与业务用途绑定

> 状态：E02-A completed locally；E02-B1/B2/B3/B4/B5/B6/B7/B8/B9/B10 completed locally；完整E02继续执行，完整用途切换关闭。用户已授权持续推进和提交，不分配发布版本。
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
