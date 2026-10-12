# AI 技术范式雷达：GitHub 云端自动化上线清单

独立单路线模型验收使用 `one-route-acceptance.yml`，不使用正式工作流的 seed 伪装缩小研究范围。默认不授权、无定时触发、无 SMTP/生产状态/续跑能力；显式授权最多一条路线、20 元内，按核验当日的北京 Qwen 官方价格做请求前预留与用量结算，未知扣费停止。其他付费平台关闭，不能冒称整周完成。详见 [单路线验收](ONE_ROUTE_ACCEPTANCE.md)。

2026-10-12 真实样本暴露默认思考挤占 JSON 总输出空间：新增 `LLM_ENABLE_THINKING` Variable，默认 false，正式/Smoke 客户端对已核验的北京 Qwen 3.7 Plus 显式设置模式，并将输出上限统一为思考与回答总和；单路线验收固定 false、不受生产 Variable 影响。保留原模型、各阶段 tokens 与时限，不用涨预算掩盖截断。模式变化重开旧模型检查点。首次样本与保守费用见 [模型复核](RUN_38235929382_MODEL_REVIEW.md)。

实际生产状态副本已完成 v8→v9 迁移：10 张原表的数量与逐行摘要全部守恒，新额度表为空，迁移前后 quick_check=ok。仅验证 schema 迁移，不等于真实待办研究闭合；原 ZIP/云端库未修改。见 [实库迁移证据](STATE_V9_REPLAY_20261012.md)。

## 当前交付与恢复契约（2026-10-12）

正式报告必须同时满足研究任务与已配置信源覆盖闭合；预算延期不是结论。任一待分析、待深挖、执行错误或人物/URL 缺口使主流程非零退出，只保存内部检查点与审计，不生成或发送阶段性 memo。完整空雷达仅表示原窗口、约定范围内没有合格路线。旧阶段性 outbox 隔离，不覆盖较新的候选；`--report` 只能续投合规任务或重建已闭合的历史报告。

当前 schema v9：保留 v8 的 `research_campaigns`、`campaign_work`、`research_attempts`、`research_stage_cache` 及 v7 来源/执行边界，新增 `provider_credit_periods` / `provider_credit_requests`。恢复 v1–v8 在下载副本迁移，保留原点、候选、交付历史，不清库；v9 缺表、损坏额度账本、无效批次/任务身份或领域 payload 拒绝恢复并尝试较早健康快照。回退需指定兼容状态并对账最新交付与费用，不能让旧代码写新库，不能靠旧 artifact 遗失已确认交付/扣费。

`TAVILY_MONTHLY_CREDIT_LIMIT=1000` 默认启用月度保护，`0` 明确关闭它。生产进程先查询账户真实 UTC 月份用量，basic 请求前在 SQLite 原子预留，未知扣费不退款；账本随状态上传/恢复，额度或用量未知使对应候选 pending，不能假称零讨论或自动扩支出。它不是同账户所有客户端的原子费用保证，提供方计数延迟/旧状态回退仍需 PAYG 关闭或账户硬限额。独立 Smoke/信源样本不恢复生产账本，先 `/usage` 再最多一个付费搜索，仍须明确授权。

首跑冻结研究窗口、seed、发现快照和历史刷新清单。未完成时下一次正常运行自动恢复该批次；发现重试失败或解析契约已变化的来源，已检查的历史路线不每次重复入队。公开入口/查询/窗口的 scope 签名与适配器契约签名分离：同一 scope 下修复解析器可重新核验，不能删除失败来源、改成未配置或变更原窗口获得覆盖完成凭据。旧快照没有独立 scope 时，官方入口须有完全一致的逐页面清单及已记录窗口，数量相等不能代替证明。原窗口之后的发布与指标观测不能倒填为本期事件。已有历史 backlog 仍进入清单，不能缩分母制造闭合；有故障的具体请求/入口不会因数量占比较小而变成完成。

`python main.py --resume-research` 严格只恢复活动批次，不建新窗口；`--expected-campaign-id ID` 额外校验身份，不匹配在研究/续投前失败。Actions 的 `resume_campaign_id` 用于同一控制，必须同时 `smoke_only=false`、`reset_state=false`。关闭或更改窗口不会改变已经创建的批次。交付确认后才能建立新窗口；成功恢复旧 outbox 后仍先上传新状态，再独立排队新研究，不能叠在一个 90 分钟进程。

研究按本期/更新/补课 6:2:1 实际耗时轮转。普通资格预筛默认 24 条，完整抽取默认 6 条，独立路线深挖默认并发 2；数量不是 Top-K。长报告每个成功机制立即保存下游候选与父进度，健康且有进展的分片在同一运行重新排队。人物轨迹逐人缓存成功结果；输入/身份、提示词/Rubric 与模型配置变化使缓存失效。活动批次中同版本综合/人物检查点可跨周复用，批次外仍是 48 小时；社区临时正文不因此落盘，增强未进入综合检查点仍须重取。并发阶段异常/取消会等待其他在途任务结束，避免后台请求继续耗费。

普通预筛现在有界前瞻填充逐原点缓存，与小批完整抽取解耦，不因穿插高优先级原点而经常只有 4—6 条；执行顺序/门槛不改变，失败身份同次访问不重复付费补齐。刷新来源已过期时先转交可恢复深挖，父任务不删；完整新版本深挖完成前不能取得刷新完成凭据。

模型请求耗时按阶段记录，请求秒数之和因并发可能超过墙上时间。预算取消仍传播，并记录 `usage_reported=false`；取消客户端等待不证明服务端没有执行或收费。未知用量会阻止自动预算续跑，不能把原先只有 86 条成功返回记录视为全部付费调用。该耗时修复本身不改变模型/SDK；本页最新的显式 Qwen 模式与总输出策略是后续真实样本触发的独立修复。

OpenReview 现在按固定窗口、venue、query 和页大小保存已转换的一手证据与分页位置；先提交健康页快照，再登记 query 完成。中途 429/取消不丢失前页；同批次续跑只补尾页，完成的零命中 query 同样有回执。官方详情新增同窗口只增不减清单与逐 URL 的正文/执行回执，先保存结果再暴露完成；显式 safety limit 的尾项与失败项跨进程保留，已完成详情不重复 HTTP。轻量父清单与独立子记录仍使用 v9 的 `research_stage_cache`，旧 inline v1 清单先保存全部子项再升级 v2；迁移失败保留原父快照。目录仍须重新核验，其他来源也未全部具备页级恢复，不宣称全来源不重复请求。

NYU 书目回执与详情按当前策略配对恢复，不再每次重置已核验条目；旧策略或没有下游结果仍重开。正文先提交，书目核验后确认；上游确认失败可从已保存正文补齐而不重抓，未尝试/身份不匹配记录仍阻断闭合。该恢复使用原状态，不要求 reset，也不自动启用额外 OpenAlex 搜索或放大请求额度。

## 逐入口信源验收（先于完整研究）

手动 `source_check_only=true` **优先于 `smoke_only`**：跳过配置 doctor、Smoke、生产状态恢复/保存、研究、报告、全部邮件路径和续跑排队；仍执行离线发布门和 artifact 上传。只检查公开 HTTP 输入、真实生产解析器及临时 SQLite 读写回环；OpenAlex/GitHub 使用既有只读凭据。脚本默认 900 秒、300 次全局请求、45 秒单入口、6 次单入口请求；生产官方页与验收共用解压后的普通响应 10 MB、真实 PDF 文件签名响应 40 MB 上限。429/配额耗尽后同主机剩余项记为未执行。

独立 CLI 在请求前仍复用零网络配置语法检查，不要求模型或 SMTP 密钥；`flase` 等无效开关、非法入口或越界整数不会被静默解析为关闭/默认值。

在 `paradigm-radar-audit-*` 中下载 `source_audit_latest.json`、`source_audit_latest.md` 和 `source_audit.log`。HTTP 200 登录页、解析空索引、错误 JSON、详情失败都会明确失败；原始正文、查询凭据和生产数据库不进入验收制品。手动 `source_platform_checks` 默认 false；显式 true 才授权已启用平台的最小协议样本：Tavily 最多一次 basic 搜索，月度保护开启时先只读账户用量；X/Semantic Scholar 各一次，已批准 Reddit 先 OAuth 再一次搜索。Tavily/X 可能消耗额度，不调用研究模型、不启用关闭的平台、不自动加钱。缺凭据、失败/未测试/未执行阻止验收完成，不发故障邮件。即使样本全通过，`campaign_coverage_complete` 仍为 false，不证明生产全分页/研究闭合。见 [逐入口验收](SOURCE_ACCEPTANCE.md) 与 [Key 复验指南](SOURCE_API_KEY_VERIFICATION.md)。

2026-10-10 后续 JSON v3 增加恢复尝试号/响应类型；OpenReview 公开接口 3.1 秒共享间隔，普通短 Retry-After 可在原预算内整轮该主机恢复一次，挑战/未知/长等待/重复限流继续熔断。控制台逐项诊断且失败退出前写 Step Summary。旧 v2 制品仍可用 `scripts/review_source_audits.py` 复核，scope/失败账本保留，不估计长期 SLA。适配器变化使该源旧发现回执失效，不删任务，书目缺一手链接仍阻止完成。详情见 [最新云端复核](RUN_38027283680_SOURCE_REVIEW.md)。

## 有界自动预算续跑（默认关闭）

新增控制均有安全默认值，不需要新增 Secret；本次未设置远程 Variable、未触发真实运行。额外 run 会消耗 API 额度，由仓库所有者明确开启 `PARADIGM_AUTO_RESUME_ENABLED=true` 后才自动排队：

- 健康状态 artifact 必须已经上传成功；只排原 `campaign_id`，不重置、不建新窗口。
- 只处理纯时间预算延期。覆盖故障、执行/数据异常、来源版本依赖、人物/URL 缺口、人工 safety limit、模型未返回 usage 或中断后未知用量都停止自动链，保留故障供修复；用量缺失不计作测得零。
- `PARADIGM_AUTO_RESUME_MAX_RUNS=4` 含首跑，范围 1—20。
- `PARADIGM_AUTO_RESUME_TOTAL_BUDGET_SECONDS=14400` 按每次研究软预算保守预留；加入下一轮会超限时不排队。它不包含报告/安装时间。
- `PARADIGM_AUTO_RESUME_MAX_TOKENS=4000000` 是已确认累计 tokens 的轮间停止线，不是单请求费用硬上限，单轮可以越线。硬中断造成未记账用量时不自动追加支出。
- 未闭合运行仍显示失败；排队成功不能把它改成成功研究或发送阶段性报告。`research_continuation.json/log` 记录停止/排队原因，dispatch 故障有独立告警归因。

手动续跑不受上述自动链上限阻止，但仍受单次软预算和全部交付闸门约束；先检查失败原因再运行，不用反复重跑消耗额度。状态、窗口及结果可通过 `--inspect-state` 查看；该命令不联网/不研究模型，但旧库初始化可以迁移 schema，不应将它当作字节级只读数据库工具。

## 版本与验收

安装依赖前记录 commit、工作区标记、源码指纹与 Python 版本；审计和 pipeline_result 使用同一指纹，排除 Secret、数据库与生成报告。本地未提交/未推送代码不会影响 Actions。9 月 25 日、10 月 2 日运行旧提交 `c2cf688`，10 月新版日志执行 `2a979b5`；详见 [工程体检](PROJECT_HEALTH_REPORT.md)，不能混用旧日志判定新修复。

当前是工程候选，不是已经验证的生产 V0。新附件执行 `9031490`，云端 v8 恢复和状态上传确已成功；补充同 run audit/DB 后已验证副本守恒和真实积压组成，修复了过期来源仍付费刷新等无效开销。旧审计缺少逐调用耗时与取消账本，不作完整费用证明。公开源样本已运行，模型/完整研究/SMTP 未在本地调用，官方/KOL 403/404 仍未全部通过等价替代验证。不能用删除入口、重置库、取消预算或绿 Smoke 代替完成证据。

## 运行结果

- 每周五 09:15（Asia/Shanghai）由 GitHub 云端运行，本机无需开机。
- GitHub Actions 页面提供“Run workflow”按钮，可以随时手动执行，并选择 7/30/60/90 天窗口或填写精确 arXiv ID。
- 手动运行还可以勾选 `reset_state`，强制忽略旧数据库。
- `smoke_only=true` 时只做小成本真实能力验证：Qwen 必须按契约回复 `OK`；arXiv、Hugging Face、OpenAlex、GitHub 等单端点能力只发一个最小请求，Follow Builders、OpenReview、官方研究页、LessWrong/KOL RSS 最多按配置顺序 failover 5 个入口并在首个成功后停止；Tavily 只消耗一个 basic request，SMTP 只登录不发信。每项默认 30 秒总时限，不会调用生产召回器、生产解析器、SQLite 持久化回环、报告或邮件发送，因此绿色 Smoke 只是完整运行的前置条件。
- 定时触发和关闭两种检查开关的手动触发执行 `python main.py`；只有完整研究闸门与报告质量闸门通过后才发送邮件。`source_check_only` / `smoke_only` 不进入正式研究。
- 研究检查点、报告渲染和邮件投递已经解耦。研究完成后先写入持久化 outbox；报告或 SMTP 失败不会撤销已完成研究，也不会把该报告登记成已成功交付。缺少当前输入契约的冻结快照，或完成内置修订后仍违反确定性交付质量契约的制品会立即隔离；网络、超时等其他渲染失败默认累计 3 次后隔离，并把候选原子退回 `pending_deep`。隔离记录不再阻塞后续研究。SMTP 失败仍保留已验证报告并让必需邮件任务失败。成功续投旧 outbox 后当前进程安全退出，云端在新状态 artifact 上传成功后自动排队第二个完整研究 run；若旧任务被隔离，当前进程直接继续新研究。若本轮新生成的报告被隔离，状态 artifact 仍会保存，但当前 Workflow 必须失败，不能把未完成交付显示为绿色；下一次运行也不会再被该坏任务阻塞。
- 报告若包含英文长段、评分表、字段拼装、绝对化“新范式”宣称、缺少关键人物/公开检索记录或缺少任一路线的一手链接，会先自动重写一次；仍不合格则任务失败且不发送邮件。人物名会先排除邮箱、URL、TeX/版式残片和拼接联系方式；OpenAlex 同名结果必须由当前论文题目交叉核验，只有具备最低背景、联系入口检索记录和直接身份锚点/当前工作对齐的人物才进入确定性索引。人物与原文索引由结构化证据确定性生成，不依赖模型抄写；全文每一个 HTTP(S) URL 都必须原样来自证据或人物档案，一个有效原文不能掩盖另一个猜测链接。
- 成功邮件除研究 Memo 外，还会附带本轮结构化筛选审计和运行日志；审计记录信源返回量、筛选理由及各阶段 token 用量，不保存 prompt、模型正文或私有推理。
- 去重数据库会在生产运行结束后以 `always()` 语义保存为私有 Actions artifact，包括报告/SMTP 失败后留下的研究检查点与 outbox。当前 schema v9 除 SQLite `quick_check`、表/字段迁移、领域 JSON 与 fingerprint/key 核验外，还会检查额度账本、来源/执行检查点一致性，以及活动 outbox 是否满足当前人物/一手来源输入契约；不可再交付的旧任务在迁移时隔离并把候选退回深挖。证据在写库事务之前逐条执行与恢复路径一致的领域回环校验，结构不合法的记录按来源隔离并把本轮计入研究积压，健康同批记录仍会提交。最新 artifact 即使数据库页完整、但领域 payload 已损坏，也会被拒绝并继续尝试更早的不可变快照。恢复成功后会执行 `python main.py --inspect-state`，在日志中输出不含候选正文和 Secret 的队列计数、截断 key、失败类型及尝试次数。除非手动勾选 `reset_state=true`，所有快照均缺失、损坏或不兼容都会 fail closed，不会静默冷启动。
- 工作流先在清空生产配置、拒绝网络和忽略 `.env` 的子进程中运行完整离线单元测试、无第三方包的失败提醒导入检查与静态编译，再接触生产状态和真实接口。任一普通生产步骤失败时，最后的 `always() && failure()` 步骤会尝试发送独立失败提醒，并指出首个失败步骤、运行 commit 与离线失败用例；不会再把回归测试失败误报成研究主流程失败。GitHub 直接取消整个 job、Runner 宕机或达到 90 分钟硬超时时，任何后置步骤都无法保证执行，因此必须依靠软预算主动收尾。正式报告必须通过完整研究闸门：待分析、待深挖、执行异常、人物/链接缺口或已配置召回入口未闭合时，主流程非零退出，保存检查点但不生成/发送报告。仅在研究与覆盖均闭合后，0 条交付才是本期约定范围内的有效空结论。
- `logs/pipeline_result.json` 区分 `fresh_research_executed` 与 `fresh_research_completed`：后者只有在本轮确实执行研究且 `research_incomplete=false`、`coverage_incomplete=false` 时为真；恢复旧邮件不算新研究；未闭合运行写入 `result_kind=research_blocked` 和 `delivery_blocking_reasons`，并明确失败。审计另含原点/深挖队列运行前后总量、净变化、最长待办龄，以及本期发布日期原点与深挖路线各自的计划/完成量；本期完成为零时检查 `current_window_origin_service` 与 `current_window_deep_service` 事件，区分被历史积压饿死和技术审查未通过。净增长需要结合实际来源到达量评估，不能只以 Workflow 绿色判断吞吐是否足够。

## 1. 私有 GitHub 仓库

项目应保存在 **Private** GitHub 仓库。2026-09-23 只读检查 GitHub 仓库页面时显示 **Public**；这与本清单的隐私假设不符，需由仓库管理员核对并决定可见性，不由研究代码自动修改。推送前务必确认 `.env` 和 `*.db` 没有进入提交；它们已在 `.gitignore` 中排除。公开仓库的 Actions 日志、报告或审计输出也应按公开信息审视，不能因为 artifact 下载需要登录就视为整个运行资料私有。

工作流必须位于默认分支，GitHub 的定时与手动触发才会生效。

## 2. 配置 Actions Secrets

进入 GitHub 仓库：`Settings → Secrets and variables → Actions → Secrets`，添加：

| Secret | 是否必需 | 填写内容 |
|---|---:|---|
| `DASHSCOPE_API_KEY` | 是 | 阿里云百炼 API Key |
| `SMTP_USERNAME` | 是 | 完整 QQ 邮箱，例如 `123456789@qq.com` |
| `SMTP_PASSWORD` | 是 | QQ 邮箱生成的 SMTP 授权码，不是 QQ 密码 |
| `SMTP_TO` | 是 | 收件邮箱；多个地址用英文逗号分隔 |
| `OPENALEX_API_KEY` | 是 | OpenAlex 免费 Key；用于论文检索与作者身份核验 |
| `SEMANTIC_SCHOLAR_API_KEY` | 否 | 没有学术邮箱时不创建此 Secret |
| `TWITTER_BEARER_TOKEN` | 否 | X API Bearer Token；用于按工作标题搜索作者本人/KOL 的近期帖子 |
| `TAVILY_API_KEY` | 推荐 | Tavily 普通账号的 API Key；用于发现公开索引的社区页面和独立技术博客 |
| `REDDIT_CLIENT_ID` | 否 | Reddit 批准 Data API 访问后创建的 OAuth Client ID |
| `REDDIT_CLIENT_SECRET` | 否 | 对应 OAuth Client Secret；不得放在 Variable |

不要创建名为 `GITHUB_TOKEN` 的 Secret。GitHub 会为每次运行自动提供权限受限的临时 token，工作流已直接使用。工作流声明 `actions: write` 用于在历史续投完成，或已授权的纯预算续跑满足条件且状态上传成功后触发 `workflow_dispatch`；代码仍是 `contents: read`，不会推送或改写仓库。

## 3. 配置 Actions Variables

同一页面切换到 `Variables`，按需添加：

| Variable | 填写内容 |
|---|---|
| `LLM_REQUEST_TIMEOUT_SECONDS` | 推荐 `180`；单次模型请求硬上限，业务层另有一次可审计重试 |
| `LLM_ENABLE_THINKING` | 默认 `false`；仅已核验官方 Qwen 配置，显式模式且总输出受限，修改后模型缓存失效 |
| `SMOKE_CHECK_TIMEOUT_SECONDS` | 推荐 `30`；每个真实接口冒烟的总时限，且冒烟不执行生产分页 |
| `OPENREVIEW_VENUES` | 逗号分隔的 venue id |
| `SEMANTIC_SCHOLAR_ENABLED` | 没有获批 Key 时填 `false`；只有同时创建 Key Secret 时才填 `true` |
| `RESEARCH_FEED_URLS` | 已验证的官方 RSS/Atom 地址，逗号分隔 |
| `LESSWRONG_SOURCE_ENABLED` | 可选；默认 `true`，使用公开官方 RSS，无需 Secret |
| `LESSWRONG_KARMA_THRESHOLD` | 可选；默认 `30`，只表示 frontpage Feed 的 karma 入选下限 |
| `ALIGNMENT_FORUM_SOURCE_ENABLED` | 可选；默认 `true`，使用公开官方 RSS |
| `KOL_SOURCE_ENABLED` | 可选；默认 `true`，读取代码内手工核验的个人 Feed |
| `KOL_CUSTOM_FEED_URLS` | 可选自定义 Feed；因身份未内置核验，默认只作二次解读 |
| `KOL_X_SOURCE_ENABLED` | 可选；默认 `false`。显式 `true` 且配置 Bearer Token 才调用 KOL/社区标题搜索/Smoke；既有 `true` Variable 需由所有者改为 `false` |
| `TAVILY_SOCIAL_SEARCH_ENABLED` | 可选；默认 true，正式网页索引开关；本轮补齐 Workflow 注入，免费与否需核对提供方套餐 |
| `TAVILY_SOCIAL_SEARCH_DOMAINS` | 可选；平台 Smoke 样本域名；正式组合检索用 `TAVILY_DISCOVERY_DOMAINS`，不是全 X API |
| `RESEARCH_WATCHLIST_MODE` | 推荐 `merge`；自定义值追加到仓库内置目录，只有明确需要时才用 `replace` |
| `PRIORITY_RESEARCH_PAGES` | 可选的额外官方研究索引；留空使用内置海内外目录，追加页面不会自动成为权威背书 |
| `PRIORITY_RESEARCH_LINK_SAFETY_LIMIT` | 官方入口 HTTP 抓取熔断；默认 `0`，读取日期窗口内全部候选链接 |
| `PRIORITY_RESEARCH_CONCURRENCY` | 官方研究页并发数，默认 `6`，不建议超过 `12` |
| `PRIORITY_BIBLIOGRAPHY_RESOLUTION_ENABLED` | 默认 `false`；显式开启才用 OpenAlex 补无标识书目，需已配置 Key；关闭时未解析项仍阻断覆盖 |
| `PRIORITY_BIBLIOGRAPHY_REQUEST_LIMIT` | 每个生产进程新增身份搜索上限，默认 `8`，范围 `1`–`100`；成功身份随数据库续跑，不是月度费用上限 |
| `ESTABLISHED_RESEARCH_ORGANIZATIONS` | 可选的额外已核验组织/具名实验室别名；禁止填整所大学和歧义短词 |
| `MONITORED_RESEARCH_ORGANIZATIONS` | 可选的额外监测组织；只增加关注，不因品牌自动放行 |
| `PRIORITY_RESEARCHERS` | 可选的额外重点研究者姓名；必须与公开主页或学术 ID 联合核验 |
| `REDDIT_API_ACCESS_APPROVED` | 只有收到 Reddit 批准且用途符合许可时填 `true`；否则保持空或 `false` |
| `REDDIT_USER_AGENT` | 例如 `python:ai-paradigm-radar:v1.0 (by /u/你的用户名)`；不含密钥，可放 Variable |
| `TAVILY_REQUEST_SAFETY_LIMIT` | 每进程请求熔断；默认 `0` 不限，不等于关闭月度保护 |
| `TAVILY_MONTHLY_CREDIT_LIMIT` | 默认 `1000`，非负整数；月度保守 credit 停止线，`0` 明确关闭保护；未知用量不继续 |
| `TAVILY_DISCOVERY_DOMAINS` | 默认留空执行全网发现；只有要限制 Tavily 站点时才填逗号分隔域名 |
| `PARADIGM_RECALL_OVERLAP_DAYS` | 推荐 `30`；只用于 Technical Report、重点研究者和官方研究入口的重叠回补，普通发现与交付新鲜度仍按 `SOURCING_LOOKBACK_DAYS` |
| `PARADIGM_PRIORITY_AUTHOR_SWEEP_ENABLED` | 推荐 `true`；启用与领域术语独立的重点研究者 arXiv 召回车道 |
| `PARADIGM_MIN_SUBSTANTIVE_DISCUSSIONS` | 默认 `2`；把可核验的非作者实质讨论条数映射到客观 Rubric 选项，不是主观分数 |
| `PARADIGM_MIN_SECONDARY_ENGAGEMENT` | 默认 `50`；未知团队外部承接的互动量客观边界，需结合独立性与平台覆盖解释 |
| `PARADIGM_BOOTSTRAP_LOOKBACK_DAYS` | 推荐 `60`；状态数据库为空或覆盖地图版本变化时，只扩大上述高信号车道 |
| `PARADIGM_RESEARCHER_PROFILE_LIMIT` | 推荐 `6`；兼容性人物档案安全上限，完整作者名单仍保留在证据中 |
| `PARADIGM_KEY_RESEARCHER_LIMIT` | 推荐 `3`；实际核验并写入报告的一作、通讯/资深作者或重点研究者上限 |
| `PARADIGM_*_SAFETY_LIMIT` | 可选运行熔断；默认/推荐 `0`，表示数量完全由 Rubric 结果决定 |
| `PARADIGM_RUN_BUDGET_SECONDS` | 推荐 `3600`；与 1200 秒报告上限合计 80 分钟，给安装、测试、邮件和 artifact 留约 10 分钟；Actions 中不能设为 `0` |
| `PARADIGM_STAGE_RESERVE_SECONDS` | 推荐 `1200`；发现完成后从实际剩余研究预算中为深挖保留的目标余量 |
| `PARADIGM_DISCOVERY_SOURCE_TIMEOUT_SECONDS` | 推荐 `600`；单个并行发现源的墙上时间上限，超时只降级该来源并标记覆盖未闭合 |
| `PARADIGM_REPORT_TIMEOUT_SECONDS` | 推荐 `1200`；研究快照入 outbox 后，研究总编辑渲染的独立上限 |
| `PARADIGM_REPORT_REQUEST_TIMEOUT_SECONDS` | 推荐 `360`；单条路线或轻量总编框架的一次模型请求上限，不再沿用分析请求的 180 秒 |
| `PARADIGM_REPORT_ROUTE_CONCURRENCY` | 推荐 `2`；路线级写作并发，每条通过后立即写入状态 artifact 可恢复的 checkpoint |
| `PARADIGM_REPORT_MAX_RENDER_ATTEMPTS` | 推荐 `3`；持久化渲染重试上限，达到后隔离任务并把候选退回深挖，不影响 SMTP 重试 |
| `PARADIGM_ANALYSIS_BATCH_SIZE` | 推荐 `6`；机制抽取检查点粒度，不是候选上限 |
| `PARADIGM_ORIGIN_PREFILTER_ENABLED` | 推荐 `true`；普通论文先做保守的批量资格判断，任何不确定或漏回项仍进入完整 Rubric/保留 pending，不是 Top-K |
| `PARADIGM_ORIGIN_PREFILTER_BATCH_SIZE` | 默认 `24`，范围 1—32；普通资格预筛粒度，不改变完整 Rubric |
| `PARADIGM_DEEP_CONCURRENCY` | 默认 `2`，范围 1—4；独立路线端到端研究并发 |
| `PARADIGM_AUTO_RESUME_ENABLED` | 默认 `false`；额外 API 支出需所有者明确开启，只有纯预算延期可排队 |
| `PARADIGM_AUTO_RESUME_MAX_RUNS` | 默认 `4`，范围 1—20；包含首跑的批次自动链次数上限 |
| `PARADIGM_AUTO_RESUME_TOTAL_BUDGET_SECONDS` | 默认 `14400`；研究时间保守预留总额，不含报告/安装 |
| `PARADIGM_AUTO_RESUME_MAX_TOKENS` | 默认 `4000000`；已确认 tokens 轮间停止线，非请求硬费用上限 |
| `PARADIGM_TECHNICAL_REPORT_MECHANISM_SLICE` | 推荐 `2`；单份 Technical Report 每次出队评估的机制数，总机制数不设上限并跨轮续跑 |
| `PARADIGM_DEEP_BATCH_SIZE` | 推荐 `1`；深挖检查点粒度，避免半完成档案入库 |
| `EMAIL_MAX_ATTACHMENT_BYTES` | 推荐 `10000000`；报告异常膨胀时阻断发送并保留 outbox |

不配置变量时，自定义研究 RSS 为空，但 LessWrong、Alignment Forum 与内置 KOL Feed 默认启用；研究入口、组织与重点研究者使用仓库中的版本化默认目录，不影响工作流语法。若仓库已有旧版 `PRIORITY_RESEARCH_PAGES` 或 `ESTABLISHED_RESEARCH_ORGANIZATIONS` 长名单，`merge` 会保留它们并同时加载新默认目录，不会冻结后续更新。

静态体检会直接拒绝拼错的布尔值、非整数、越界并发/超时值，以及格式错误的官方页面、Feed、Follow Builders 和 OpenReview venue，而不是沿用默认值继续运行。例如 `flase`、`three-thousand`、`PARADIGM_REPORT_ROUTE_CONCURRENCY=99` 或缺少 `https://` 的页面地址都会在访问状态 artifact 和真实 API 之前失败，并在告警邮件中列出变量名而不回显可能含凭据的完整 URL。

GitHub 上通常只需添加 `TAVILY_API_KEY`；`TAVILY_DISCOVERY_DOMAINS` 留空时同时发现社区和普通技术网页，结果仍只算索引线索。LessWrong/KOL RSS、Rubric、前沿覆盖地图和个人目录都随代码提交，无需创建 Secret 或 Variable。旧版 `PARADIGM_MAX_ANALYSIS_ITEMS`、`PARADIGM_MAX_DEEP_CANDIDATES` 等 Variable 可以删除；即使保留，新代码也不会读取。Reddit 的 Client ID/Secret 必须和“已批准”开关一起配置；只填密钥但不开启批准开关时，代码不会请求 Reddit API。Semantic Scholar 同理：Secret 留空、Variable 为 `false` 时，代码不会匿名请求。

跨提交恢复状态会从新到旧校验不可变快照，跳过下载失败、压缩包损坏、缺少数据库或无法迁移的快照；找到最近一份健康 SQLite 后再迁移到当前 schema 并读取前沿覆盖地图版本。它不再因为 commit SHA 或一个可迁移的版本号变化就重置。普通 Prompt、Skill 或报告样式更新会延续跨周历史；覆盖地图升级时仍恢复旧数据库用于证据去重，但程序会用 60 天窗口补扫新加入的技术面，报告时效仍使用普通任务窗口。已经分析过且正文未变化的材料不会再次调用 LLM。旧状态中缺少可靠分类来源的 Technical Report 会按当前文档元数据与系统结构规则重新核验；机制队列在高信号与普通材料之间按 1:3 加权轮转，两类内部再交错新增与 FIFO backlog。报告中每条已通过闸门的路线草稿也会按候选证据与写作 Skill 签名保存；总编超时后，下次只补未完成路线并重做轻量开篇，不重烧完整路线写作。存在活动 outbox 时先恢复它：成功交付后当前进程退出，工作流上传确认状态并自动 dispatch 一个 `reset_state=false`、`smoke_only=false` 的后续 run；确定性输入缺陷或耗尽渲染重试时隔离旧任务，并在当前进程继续新研究。自动 dispatch 失败会让当前任务失败并触发告警，不能静默把续投当成本周研究。若找不到任何健康 artifact，工作流会明确失败并要求人工判断；只有确定要建立新基线时才以 `reset_state=true` 运行。

细粒度续跑与批次数据契约以上文为准。源/模型失败保留可执行对象与尝试账本，不能只留下一个没有队列对象的 pending 计数。报告/SMTP 故障不回滚研究；确认点崩溃时 SMTP 仍只能提供可审计的至少一次重试，不能承诺跨服务严格 exactly-once。

## 4. 首次手动验收

1. 打开仓库的 `Actions`。
2. 选择 `AI 技术范式雷达`。
3. 点击 `Run workflow`，第一次选择 `7` 天，保持 `smoke_only=true`。精确 arXiv ID 留空，此时 `reset_state` 不影响结果。
4. 确认“配置体检”和“小成本真实接口冒烟”完成，并下载 `paradigm-radar-audit-*` 查看 `smoke_test_latest.json`。文件中的 `contract_version` 可确认线上使用的是哪版探针；`failure_kind=transient_availability` 表示第三方临时限流/超时，显示为 `degraded` 且 Workflow 可通过，但风险不会被隐藏；`multi_endpoint_unavailable` 表示某个多入口能力的有界样本全部失败；配置、真实鉴权或响应契约失败才显示为 `failed`。Smoke 不覆盖生产解析器与 SQLite 回环，必须继续执行下一步完整运行。
5. 先执行 `source_check_only=true`、`reset_state=false` 的逐入口验收（此时 `smoke_only` 被忽略）。下载 source audit，逐项处理失败/未执行/需单独授权项。样本通过不代表全分页闭合，也不能掩盖云端与本地 CDN 差异。
6. 经完整研究/邮件授权后，再把 `source_check_only` 与 `smoke_only` 都改成 `false`。已有历史状态或正在修复失败任务时保持 `reset_state=false`，让 schema v9 迁移和 outbox 恢复接管；只有新仓库完全没有可用 artifact、或人工确认要建立全新基线时才使用 `reset_state=true`。
7. 下载审计，先确认固定批次任务全部闭合、覆盖完成且报告契约通过，再确认邮件和状态保存成功。预算未闭合会失败并保留状态；不要仅以 Workflow 绿色验收。
8. 确认收件箱收到邮件及运行审计附件，并在该次运行的 Artifacts 中看到报告、`paradigm-radar-state-<run_id>` 与 `paradigm-radar-audit-*`。状态按运行保存为不可变快照；下一次任务恢复最近一份未过期快照，同时兼容旧的 `paradigm-radar-state` 名称。

首次成功后不需要再保持电脑开机。以后每周五由 GitHub 执行；网页手动运行和定时运行共享同一份去重状态。

## 5. 运维注意事项

- 不要同时长期运行本机 `python main.py --schedule` 或重复的 Codex 自动任务，否则可能在同一天收到两封邮件。
- Actions 显式使用 `submodules: false`：范式流水线读取远程 Feed，不依赖 `gzh_sourcing/we-mp-rss` 或 `social_media_sourcing/follow-builders` 的工作树。`.gitmodules` 仍由离线契约检查 path/URL 完整性，避免历史辅助目录再次让 checkout 因缺失 URL 失败。
- GitHub 定时任务可能因平台负载稍有延迟，所以安排在 09:15 而不是整点。
- 状态和报告 artifact 当前保留 90 天；失败运行只要产生了可校验数据库也会上传，因此报告/SMTP 故障不会迫使下一次重新烧掉整轮研究 tokens。
- 依赖安装、离线回归、配置体检、状态恢复、主流程、状态/报告/审计上传都有稳定 step id。配置在下载或覆盖跨周状态前 fail fast。失败提醒会报告第一处失败而不是笼统归因给 pipeline；审计 artifact 同时保留 `dependencies.log`、`offline_checks.json/log`、`doctor.log` 和状态准备/恢复日志。普通步骤失败时会尽量保留 `current_run.log`、结构化审计、outbox 和包含 Actions 链接的告警。硬取消/Runner 故障仍是平台边界，不能承诺后置步骤执行。
- 三类 artifact 都使用包含 `run_id` 的不可变名称；状态恢复选择最近一份未过期快照。这样新上传失败不会先删除上一份健康状态，重新运行也不会与旧 attempt 争抢同名 artifact。
- 如果连续超过 90 天没有任何可用状态 artifact，下一次生产运行会 fail closed，不会自行当作首跑。人工确认后使用 `reset_state=true` 创建新基线。
- 代码 commit 变化不会自动丢弃旧状态；兼容的数据库 schema 会迁移，已退役的 JSON 字段会忽略，字段类型错误、不可解析 JSON、key/fingerprint 不一致则回退上一份快照。若日志出现“证据状态字段 `published_at` 必须是字符串”等领域类型错误，优先检查来源适配器是否把 JSON null 直接写库，不要归因于 Secrets，也不要用 `reset_state=true` 掩盖。所有快照均损坏或超出兼容范围时任务会失败，只有用户明确选择 `reset_state` 才冷启动。覆盖地图版本变化会保留旧去重历史并扩大为 60 天补扫。V0 阶段确需清空所有历史时才手动勾选 `reset_state`。
- 所有计划研究及已配置信源覆盖均闭合后，没有合格路线可以发送完整空雷达。存在 backlog、执行异常或覆盖缺口时禁止正式报告，不能改写提示、清空统计或把观察池更名来制造完成。失败只保留研究检查点和内部审计，独立故障提醒不附带阶段性研究 memo。
- GitHub 公共仓库连续 60 天无活动可能停用 scheduled workflow，因此本项目建议使用私有仓库。
- 修改工作流后，确保更改已经进入默认分支。
- 2026-10-12：正式研究会原子重开明确社区失败却已终结的旧候选，保留交付历史；只读/信源检查不执行。新版候选有社区执行回执，失败对象阻断综合/交付，健康对象继续；研究状态保存仍先于完成标记，禁止阶段性邮件。ModelBest 分页及 NYU 身份清单使用研究阶段缓存。公开 audit 不隐式获得书目 API 授权，详见 [输入闭合修复](INPUT_CLOSURE_REPAIR_20261012.md)。
- 工作流使用 Node 24 版本的 `checkout@v6`、`setup-python@v6` 与 `upload-artifact@v7`；任务不执行 git push，因此 checkout 不持久化临时凭据。
