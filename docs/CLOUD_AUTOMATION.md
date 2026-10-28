# GitHub 云端运行与 V0 验收

## 默认工作流

weekly-radar.yml 每周一 08:00（Asia/Shanghai）运行（`cron: "0 8 * * 1"`、`timezone: "Asia/Shanghai"`），Ubuntu / Python 3.12，90 分钟上限。研究 3600 秒、报告 1200 秒，留出依赖、离线门、邮件和制品时间。本机不用开机。正常启动后预计 10:00 前完成，但 GitHub 调度/排队可能延迟，不能承诺精确送达时间；调度修改须部署到默认分支才生效，不因修改代码自动补跑。

定时触发会调用已配置模型及邮件；人工测试仍需用户授权。代码修复和上传本身不是新增付费测试/发信授权。

## 手动选项

| Input | 作用 |
|---|---|
| smoke_only=true | 模型最小 JSON、公开/OpenAlex/GitHub 输入能力；不调用 Tavily/X/Reddit/Semantic Scholar、SMTP，不发信或恢复生产状态 |
| source_check_only=true | 优先于 Smoke；逐入口 HTTP/生产解析/临时 SQLite 检查，无模型/SMTP/生产状态 |
| source_platform_checks=false | 默认不测付费/需批准平台；显式开启才做已启用平台的最小有界请求 |
| reset_state=false | 使用 V0 状态；首次 V0 无快照则自动建立干净 epoch |
| lookback_days | 正式普通窗口支持 3/7/30/60/90 天；3 天仍是会调用研究模型及邮件的正式运行，不是低费用保证 |
| seed_arxiv_ids | 临时材料可见性，不提高技术准入 |
| resume_campaign_id | 严格恢复指定原计划，必须关闭 Smoke 且不重置 |

正式运行需 smoke_only=false、source_check_only=false。不必等待所有官网检查全绿。

云端 smoke_only=true：每个独立模型配置最多一次请求、输出上限 64 tokens、验证完整 JSON；相同模型/地址/Key 去重，SDK 不自动重试。输出停止原因为 length 但完整返回 `{"ok":true}` 时可通过，不因预算停止单独否决；半个 JSON 或空正文仍不通过。原点召回能力门槛沿用 PARADIGM_MIN_PRIMARY_SOURCES（默认 2 类），其他入口失败留诊断，不要求全绿；模型鉴权、JSON 契约或有效输入不足仍失败。JSON 制品保留实际输入/输出/总 tokens，缺用量标 unknown，不能算费用为零。Smoke 不是正式判断或写作质量验收；不会自动串联 3 天研究。

## 模型、信源与邮件配置

Settings → Secrets and variables → Actions：

- MAIN/SUB 独立的六项 Repository Variables 必填：SUB_AGENT_PROVIDER、SUB_AGENT_MODEL、SUB_AGENT_BASE_URL；MAIN_AGENT_PROVIDER、MAIN_AGENT_MODEL、MAIN_AGENT_BASE_URL。正式/Smoke 与单路线工作流读取同一套双层配置，无全局继承、跨角色借用或硬编码兜底。纯信源检查不要求模型配置。
- 两项独立 Secret：SUB_AGENT_LLM_API_KEY、MAIN_AGENT_LLM_API_KEY；可以先填同一个现有 Key，但不能放 Variables。任一角色缺项在调用模型前明确失败。
- 旧全局模型 Variables 及 DASHSCOPE_API_KEY / LLM_API_KEY / SUB_AGENT_API_KEY / MAIN_AGENT_API_KEY 不再读取；先配置新 Secret 再删旧项。GitHub 不提供旧值读回，不在聊天或日志中迁移明文。
- 换某层提供方只修改该层三项 Variables 与对应 Secret，无需改代码，不限 Qwen。地址填兼容 API 根地址，不是 /chat/completions 完整路径。
- 可选 SUB_AGENT_REQUEST_OPTIONS / MAIN_AGENT_REQUEST_OPTIONS 已映射到正式、Smoke 与独立样本。协议默认按 PROVIDER 选择，不绑定模型 ID/固定地址；特殊兼容接口以 JSON 配置输出参数、extra_body 和省略参数，见配置清单。修改后模型缓存失效，不恢复旧参数结果。
- 当前 MAIN 使用 DeepSeek 时，关闭思考的 Repository Variable 值为 `MAIN_AGENT_REQUEST_OPTIONS={"extra_body":{"thinking":{"type":"disabled"}}}`；不要只设共享 `LLM_ENABLE_THINKING=false` 或继续使用 low。此项不改 SUB、不改 Key、不提高 Token 上限；修改 Variable 不会自动触发研究，实际效果在下一次授权运行的审计中验证。
- 推荐 OPENALEX_API_KEY；GITHUB_TOKEN 自动取 github.token，不另建同名 Secret。
- SMTP_USERNAME、SMTP_PASSWORD、SMTP_TO Secrets；现有 QQ SSL 发送路径不变。
- X、未批准 Reddit、未批准 Semantic Scholar 保持关闭，不为 V0 新增付费平台。
- 参数与范围详见 [配置清单](CONFIGURATION_CHECKLIST.md)。已有 30/60 天回补仍是有效覆盖，不在清理时静默缩到 14 天；只有所有者明确选择使用默认时才改值/删除。自动预算续跑保持 false。
- Variables 只在工作流显式映射时生效；不必把全部默认值建成 Repository Variables。FOLLOW_BUILDERS_ENABLED/FEED_URL、TAVILY_SOCIAL_MAX_RESULTS、PARADIGM_ALLOW_UPDATES、LOG_LEVEL 支持按需覆盖。旧 TAVILY_MAX_REQUESTS_PER_RUN 使用现行 TAVILY_REQUEST_SAFETY_LIMIT；旧 PRIORITY_RESEARCH_MAX_LINKS_PER_PAGE 不再消费，不能当新详情参数的别名。
- SOURCING_LOOKBACK_DAYS 与 PARADIGM_SEED_ARXIV_IDS 来自手动 inputs；SCHEDULE_* 仅本地调度/日期，不修改云端 cron。当前 QQ 邮件策略、PIPELINE_MODE=paradigm 固定，不读取同名 Repository Variables。

## V0 状态 epoch

当前 schema v10 增加 superseded 研究计划与范围交付凭据，保留此前检查点、outbox 和 credit 数据结构。

云端只恢复 paradigm-radar-v0-state-<run_id>。第一次没有这种快照时从空库启动，旧 paradigm-radar-state-* 未删除但不再导入旧失败队列。用户已允许丢弃无效历史，这里用独立 namespace 实现，不直接删除旧制品。

每次运行后以不可变 run_id 快照保存。下载副本先执行 quick_check、schema/领域对象/任务/额度/链接契约，再覆盖工作库；较新快照损坏可回退较早健康 V0 快照。已有 V0 快照全部不可用则失败，不静默冷启动。reset_state=true 是显式重建，不是普通报错解决方法。

本次失败状态不必清空：Technical Report 旧类型和完成回执在实际恢复时复核，保持机制 ID、健康兄弟判断、交付历史和 credit 账本。无效类型不得静默改成 other，未知类型在原有机制请求内重判；不会仅为迁移追加索引请求。修复后的首轮因研究规则更新可能需要复核相关综合，不能用旧成功标记跳过。

恢复合规 outbox 优先完成报告/邮件，不回滚研究、不吞旧投递；成功续投先上传状态，再单独排队新研究，不能在同一 90 分钟进程叠加。SMTP 接受后进程硬退出仍可能按同 Message-ID 至少一次重试。

## 研究与交付

传播与发布者名气为软约束：技术合格路线不因没有知名作者、KOL 转发或二次讨论而被否决。当前 Rubric 只用技术题决定资格；普通分享/互动、独立复现/采用分别留档。即使全部补充传播查询失败，也允许基于可用一手材料继续技术研究，回执仍为失败/未知，不伪造零讨论。

历史更新不再总限 4 条：PARADIGM_RESEARCH_REFRESH_LIMIT=4 只约束无当前事件的后台轮询，0 仅关闭轮询。当前传播事件匹配的旧路线另行召回，旧 pending 以独立深挖任务加入新周；未形成路线的旧原点可再次初筛。7 天约束事件日期，不约束技术年龄；同窗续跑保持冻结范围。无需新增配置、清库或轮换 Key。

默认取材为 48 原点、4 条后台历史轮询，加上当前事件匹配的旧路线；普通 7 天/高信号 14 天。新计划按来源/领域/发布组分散，审计逐来源记录“选定/发现”。论文、官方文章和原创思想均保守预筛，品牌不豁免；正式报告逐机制核验。全部技术合格候选进入深挖，不设 Top-K；未完成保留真实待办，不以整批闭合作为交付前提。旧深挖上限不再映射到正式 Workflow。单官网/API 故障只记缺口，不同发现通道不是独立复现。

完整研究名额前现有 SUB 可先审最多 4 倍名额的资格池（默认 192 条/最多 120 秒）；记录真实用量，不承诺费用下降。选定的索引论文可公开核对 arXiv 首发，无新 Key。已选路线的修订上游按独立依赖账本补审，失败不扣住其他对象的结构修复/人物补充或正式交付。配置、cron、SMTP 和制品命名均不变，无需清库或新增 Variables。上传后仍需授权真实运行，代码验证不是付费调用/发信授权。

社区渠道执行统一判定，不允许调度层放行后综合层再以任一失败渠道拒绝。Tavily 次数保护不足会给出容量提醒而不截断深挖；2026-10-15 线上 `TAVILY_REQUEST_SAFETY_LIMIT` 已从 12 改为 0，其余月度费用保护、平台开关及自动续跑默认不变。

默认 v5 以每周交付为主目标：单条失败、未尝试、预算停止、来源覆盖不足均独立留档，不否决其他合格结果。没有可发表路线（包括模型/信源整体故障）也发送本周运行回执，不能写成技术否定或“没有重要技术”。不再发送阶段性 memo，也不声称研究计划已经完成。

delivery_scope 仍从 SQLite 核对数量、逐对象版本与入选路线自己的成功记录。发表前逐条起草、过滤失败正文，冻结实际发表子集；之后确定性组装报告，不再依赖总编调用。delivery_ready 是交付资格，research_health 是研究健康度，fresh_research_completed 不因交付成功而变真。未建 outbox 的 v3/v4 活动计划升级为 v5，历史冻结 outbox 不重写。无需更改 Variables、Key 或 reset_state。

每条路线及每个 URL 必须可追溯，人物不猜测、中文正文无评分表。单条编辑/原文问题只阻止该条发表，全部无正文则发回执。EMAIL_PUSH_REQUIRED=true 后 SMTP 失败仍使交付失败，保留 outbox；只有投递确认才更新实际发表对象的 last_reported_signature。交付成功不等于本期研究无故障或覆盖完整。

独立审阅 JSON 邮件失败不再把已交付周报翻成整轮失败；该辅助步骤显式 warning，并在 Job Summary 标明“本轮数据留存未完成”，不能宣称留存成功，也不擅自上传私有材料。正式报告 SMTP、状态持久化和运行环境故障仍是需要处理的交付/基础设施错误。

人物归因为软约束：已查未找到、未披露具名作者、接口异常或模型辅助归因不足分别记录，均不阻断已合格技术交付；报告不能因此虚构人物。技术/时效/原文/链接门和费用保护不放松。

V0 执行顺序为全部首次技术访问 → 剩余时间内的定向结构修复 → 有界人物补充；后两段耗时在审计中单列。修复不重复检索，人物阶段不能占用未访问候选的首次机会。Technical Report 切片失败后继续处理尚未访问的兄弟机制；只有索引内全部机制均已访问，部分失败才可归为局部失败。恢复旧 v4 时，仅对来源版本、失败时间区间和分机制记录均能证明的错误回执作分类修正，不清空 pending、不改历史模型判断或费用账本。

审计与 `pipeline_result.json` 的 `execution_summary` 明确执行方式（new_research/resumed_research/delivery_only/unknown）、本次新增、本期累计、本期待办和全库积压。`fresh_research_executed/completed` 仅指新发现批次；`research_executed` 另行表示进入研究路径。工作流排队条件仍使用 `recovered_delivery_only`，不因这些展示字段改变续投、调度或费用保护。短续跑不代表重新采集了全部信源。

输入清理诊断与技术判断分账：`frontier_coverage.origin_input_exclusions` 记录跨入口被拦住的非研究页/挑战页，官方目录另有 `index_exclusions`、`detail_exclusions` 与 `detail_access_issues`。明确非研究详情可缓存排除回执；挑战页仍待重试，不计技术淘汰、不因单页异常否决健康材料。正文优先文章主体，保留作者、日期和报告链接，不绕过访问护栏。

## 信源诊断与独立测试

source_check_only 采用 --v0-readiness：有至少 2 类可用一手输入样本时可通过输入能力检查，但每个 failed/not_executed/not_tested 仍保留，acceptance_complete 不会被改成 true。这不是正式研究或生产 V0 认证。

不带 --v0-readiness 的本地 scripts/source_audit.py 仍是严格全清单诊断；有缺口就非零退出。--entry/--kind 子集不能声明 V0 输入能力完成。诊断不恢复生产库，不调用模型或邮件，不发送失败提醒。

source_platform_checks 只有单次明确授权后才开启；Tavily/X 可能消耗额度，不自动加钱或绕过挑战。401 检查 Key，403 区分权限/网络护栏，429 区分频率/额度，404/解析空目录检查路径/格式，不整体轮换所有 Secrets。见 [来源诊断](SOURCE_ACCEPTANCE.md)、[Key 指南](SOURCE_API_KEY_VERIFICATION.md)。

独立 source-interface-acceptance.yml 仅验授权 NYU/Tavily，默认 live_authorized=false，无模型/邮件/生产状态。NYU 最多 8 次身份 GET，Tavily 在包含额度/PAYG 条件明确后最多 1 次 basic；未知限额不搜索。2026-10-13 单次授权已经执行，后续用户要求暂不调用，不自动追加复验。

one-route-acceptance.yml 默认 3 天，两层合计最多 20 元、无邮件/生产状态。正常研究的 arxiv_id 与 primary_url 必须且只能选一项；后者须在已登记官方索引原样出现，生产解析器核验正文与原始日期。旧材料、仅修改日期、无日期不支付研究模型；拒绝多机制 Technical Report，不截断或放宽准入。`models_only=true` 则两项原点都留空，只各发一次最小模型请求，不调用其他信源。

价格保护通过 `price_contracts` 输入 sub/main 两角色 JSON 与 `pricing_verified_on`，不含代码日期或模型白名单。实际模型、地址、最高原价、完整输入容量、总输出参数/误差与响应模型别名须匹配；核验日期必须是运行当天。客户端共享一个账本与停止线，未知用量/取消不退款，不自动重跑。价格数据配置不限制生产模型选择。`identity_requests=0–8` 显式允许单路线 OpenAlex 人物身份查询；模型冒烟不注入该 Key，其他收费平台/SMTP 均不注入。研究采用 V0 部分来源容错；事实质量门保留，人物归因尽力而为且不虚构。完整输入格式见[样本指南](ONE_ROUTE_ACCEPTANCE.md)。

历史 Runner 证据见 [来源](RUN_38347140838_SOURCE_REVIEW.md)、[接口](RUN_38370843944_INTERFACE_REVIEW.md)。当时全入口闭合目标不再适用 V0 交付。

## 制品、权限与故障

- paradigm-radar-v0-state-<run_id>：数据库及 state-meta.json，90 天保留。
- paradigm-radar-audit-<run_id>：去敏审计、current_run.log、离线门、Doctor、状态恢复/准备诊断。
- 本期报告制品：合规 Markdown，报告/邮件失败不丢研究检查点。
- 独立接口/单路线制品使用各自 namespace，不污染生产状态。
- 新增审阅数据不再上传 GitHub Artifact：默认 Runner 本地保存 JSON，`mail_reviews` 在研究后独立用现有 SMTP/SMTP_TO 寄送 `research-reviews-<run_id>-<attempt>.zip`。邮件只是数据副本，不是阶段性研究报告；ZIP 未加密，须私人保存。无需归档 Secret 或初始化输入；历史累计和人工反馈由用户电脑上离线 merge 保留，不回传云端。详见 [JSON 留存](RESEARCH_ARCHIVE.md)。

研究/报告/SMTP 普通失败后仍尝试寄出已捕获样本，再检查留存错误。数据邮件失败不回滚研究或投递，不把已发正式报告标记成未发，也不为归档追加模型调用；如已启用预算自动续跑，还需 mail_reviews 成功。附件超出 EMAIL_MAX_ATTACHMENT_BYTES 或任一 SMTP_TO 拒收时明确失败，不截断或转为云端上传。Runner 销毁后未寄出数据无法找回，邮件/本地档案是该审阅数据的保存通道；请及时下载并备份。原有三类业务制品及自动续跑的默认关闭状态不变。

contents: read；actions: write 只用于已有 outbox 恢复后的新研究或显式授权预算续跑，不推送仓库。checkout 不持久化凭据、不下载无关子模块。各步骤有稳定 id，故障通知区分依赖、配置、状态、研究、SMTP 和上传失败；平台硬取消/Runner 故障不保证后置步骤执行。

离线测试清除生产 Variables/Secrets、忽略 .env，代理拒绝网络；还隔离 GITHUB_STEP_SUMMARY/OUTPUT/ENV/STATE/PATH，防止 fixture 污染真实 Runner 结果。

独立的 `offline-regression.yml` 在 push / pull_request / 手动触发时，用 Ubuntu 的 Python 3.11、3.12 各跑完整发布门。它只有 contents: read，不映射生产 Secrets，不恢复生产状态、不发信、不自动排队研究。保存的 `offline-regression-<python>-<run_id>-<attempt>` 只有去敏测试与代码身份记录，14 天保留。生产 `weekly-radar.yml` 仍先过同一完整发布门；两项 CI 通过后才进行授权正式验收，不用线上付费运行发现本可离线暴露的错误。需要平台侧强制阻止合并时，可另行把两项 CI 设为分支保护检查；代码新增 CI 不会自动修改仓库保护设置。

PARADIGM_AUTO_RESUME_ENABLED=false 默认不扩大支出。开启后只在状态上传成功、同批纯预算延期、次数/时间/已确认 tokens 未达限时排队；覆盖/执行故障与未知用量停止自动链。MAX_TOKENS 是轮间停止线，不是人民币单请求硬费用上限。

## 生产验收

部署代码后，先确认执行 SHA 与有效配置，再进行授权正式运行。从真实审计看本期选定/完成数、合格路线、用量与质量，而不是只看 Workflow 绿色。确认邮件、V0 状态上传、重复投递跳过、下一周新窗口推进。

离线通过是工程发布条件，不是真实模型质量或生产稳定性证明。最近真实测试及尚未验收边界见 [工程状态](PROJECT_HEALTH_REPORT.md)。
