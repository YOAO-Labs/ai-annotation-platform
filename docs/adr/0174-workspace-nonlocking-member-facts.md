# ADR-0174：Workspace 非锁定成员事实关系

- 状态：接受；限定本地调用边界验收通过
- 日期：2026-10-22

## 背景与决定

剩余成员 SQL 包括任意项目／用户对、全局分组、Task UPDATE 内相关 NOT EXISTS，
以及选择完整 ORM 成员和七项 Identity 资料的名册。既有 actor-only 能力或单项目
JSON 快照不能在保留这些查询和快照边界的前提下覆盖全部需求。

新增 Workspace 持有的 `aap_workspace_v1.project_member_facts` 安全屏障视图。
九个原生列及顺序复用 `PROJECT_MEMBER_COLUMNS` 和共享 `ProjectMemberRow`，便携
描述与 Native 镜像从同一类型核对。视图不加过滤、角色枚举、联接、排序或锁；PUBLIC
撤权，应用只新增 SELECT。原窄 actor 事实和成员锁能力保持各自用途。

API 使用 `aliased(ProjectMember, facts, adapt_on_names=True)`，不创建第二种 ORM
实体，不新增 merge、populate_existing 或全局 flush hook。完整查询仍复用原 identity
map，保持已加载／脏字段并刷新过期字段；局部查询保留原 Select 的 autoflush 和
局部日期解码，避免无关 year-10000 日期让 ID 查询失败。

## 消费者与事务

迁移 `member_user_ids`、通知前两次成员查询、批次 reset 的相关成员存在性、评论
提及、绩效名册、管理看板及个人绩效团队 IDs。不同调用的规则保持不同：

- 通知继续四次独立查询，分别读取普通成员、reviewer、owner 和 super-admin；
  READ COMMITTED 下语句间新提交仍可见。
- 批次判断保留原 UPDATE 内的项目职责存在性、override 和认领字段，不额外检查
  账号 active／平台职责，也不预取成员集合。
- 提及继续允许任意职责成员，保留 manager／super-admin fallback、inactive 行为
  及重复缺失 ID 的输入顺序。
- 名册保留 owner fallback、历史查询和资料字段；个人绩效分母仍只按活跃账号和
  annotator 成员关系读取，不套用通用 viewer 配对规则。

没有新增服务、DSN、连接、提交点、锁或逐行 RPC。原请求授权、写入围栏、回滚和
任务恢复流程保持。此操作不修改持久恢复协议。

## 证据与限制

在 f1090065 捕获 30 个旧行为案例，含九种 ORM 缓存状态的 autoflush 开关、独立
Select、通知语句间提交、异常日期和提及顺序；迁移后同案全部通过，不覆盖 golden。
受限 API LOGIN 的原生字段、owner／ACL、READ ONLY、无锁、同事务及已提交变更
检查通过；Node 无 Python 代理直接读取。真实 HTTP 管理列表／详情和个人绩效也在
无成员私表 SELECT 下通过，批次原相关 UPDATE、override 及外层回滚通过。

新增测试先发现 schema 缺少类型生成的标题和 keyword-only getter 调用错误；两份
schema 从共享 dataclass 重新生成，失败项最小复验通过，原始日志／退出保留。
最终 130 项不同适用案例通过，无有效失败或跳过：30 项旧行为复验、12 项受限
Native／HTTP／Node、16 项客户端／SQL 检查及 72 项实际消费者。完整案例分类和
最终检查见 `.worktree/workspace-member-facts-qualification.json`。

架构检查补充评论服务的只读客户端依赖，并约束该客户端只能依赖 SQLAlchemy、不能
导入其他应用模块；修复失败项及新增规则后，有效并集为 358 项通过、150 项既有或
设计上不适用的规则跳过。20 项 grant／验收选择单元、Ruff／格式、显式 OpenAPI
核对、文档构建及差异检查通过。

通知后两条 Project SQL、提及 manager join 和管理看板 Project 查询刻意留待后续。
`lan-production-grants.sql` 既有宽应用角色仍保留兼容私表权限；本轮只增加视图 SELECT，
受限 API 的定向撤权验收不等于默认生产／开发凭据已收紧。完整 Workspace 凭据、
冷安装／镜像、恢复和容量按[唯一台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)
集中收口。本次不切换主环境、不推送、发布、部署生产或扩展 Go。
