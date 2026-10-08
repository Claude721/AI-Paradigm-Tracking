# AI 技术范式雷达：GitHub 云端自动化上线清单

## 2026-10 完整交付约束

用户已明确拒绝阶段性研究输出。所有正式入口统一检查研究及已配置覆盖的完成记录、延期账本、完成基数、SQLite 待办与候选状态；任一未闭合，主流程非零退出，不生成或发送报告。软预算仍保存研究检查点，后续可恢复，但预算延期不是可交付结论。只允许闭合后的非空 memo 或完整空雷达，不提供开关绕过该约束。

旧阶段性或完成依据缺失的活动 outbox 会在状态迁移/续投时隔离；隔离这种旧交付不会用其冻结快照覆盖较新的候选。旧交付已确认的历史记录保留，不删除已发送事实。`--report` 只续投符合当前完整性契约的任务，或重生成最近一次已闭合报告的原始候选、统计和日期；没有这样的历史结果时明确失败，不把当前数据库计数当成已经完成本期研究。

未闭合时 `pipeline_result.json` 记录 `research_blocked` 和 `delivery_blocking_reasons`，Actions 会显示失败；`always()` 仍校验并上传健康状态与内部审计。独立失败提醒可指出故障，但不附阶段性 memo。本轮不新增自动研究 dispatch、不提高调用频率、不增加云端变量；继续 `reset_state=false`。部署前后须核对实际 commit/源码指纹，不能通过关闭软预算、清库或删除待办制造完成。

## 2026-10 执行版本与研究轮转

9 月 25 日、10 月 2 日的日志都运行旧提交 `c2cf688`，并在离线日期测试处退出。修复只有进入实际执行分支的 Git 提交后才会影响 Actions；本地工作区修改不会自动上线。更新后先检查运行 Summary 的 commit 与源码指纹，避免用旧版本的报错判断新代码。工作流在安装依赖前执行 `python -m runtime_provenance`，将 commit、工作区修改标记、源码指纹与 Python 版本写入 Summary 和 `logs/runtime_provenance.json`；离线摘要、研究审计及 pipeline_result 也包含同一运行信息。指纹排除 Secret、数据库、状态元数据与生成报告。

研究执行按本期/更新/补课 6:2:1 实际耗时轮转，空车道可借出份额。原点小批次提交后即可深挖，历史刷新不再等待整个深挖队列完成。`PARADIGM_STAGE_RESERVE_SECONDS` 仍为深挖目标预留：初始原点访问也保留下游研究空间。每次访问结果先写检查点；同一 run 新原点改变已处理路线时，旧结果从本轮输出撤出，新快照重新排队。`research_service` 给出逐车道耗时与访问次数，所有未完成对象仍计入研究积压。本轮不新增云端变量或提高定时频率；继续使用 schema v7 与 `reset_state=false`。

`candidate_input_deferred_count` 单独记录来源版本依赖未闭合的候选，不计作执行失败或预算延期。修订原点闭合后，即使没有生成新路线，也会唤醒旧候选；其来源证据原子替换、旧派生判断失效，再接受最终 Rubric。该恢复可跨进程完成，不需要清库。

## 2026-09 重构与状态迁移

项目尚未完成 V0 验收。当前 schema 为 v7，新增原点来源快照/签名和独立执行检查点；v1–v6 状态通过现有恢复流程在下载副本上迁移，保留研究完成标记、候选、outbox 和交付历史，不因升级清库。旧混合快照标为 `legacy_unverified`，后续真实来源观察只对已知机构补水差异做一次基线对齐。v7 快照若缺表、缺来源记录或视图与检查点不一致，会被拒绝并尝试较早健康快照，不能把损坏记录从增强视图静默重建。

这次升级无需新增 Secrets/Variables，也不应使用 `reset_state=true`。离线迁移回归只覆盖合成临时数据库，线上 artifact 副本验证及受控正式验收尚未执行。回退旧代码时应同时选择升级前的健康状态快照；不要让旧代码继续写 v7 数据库，否则旧写入路径无法维护新增检查点契约。workflow 仍沿用原有状态先保存、后续投/排队及邮件失败语义，本轮不调整调度频率或自动触发真实运行。

深挖的技术综合与人物核验各有原子检查点；人物成功后仍是 `pending_deep`，只有正常报告/outbox/邮件路径可以登记交付。两段缓存均最多复用 48 小时，相关支持证据、一手来源及策略变化会使其失效；这不代表已解决每位人物内部的独立续跑。人物 API 请求失败会留在待续研队列，不得把失败文案交付。该改动不需要新的云端变量或数据库迁移。

## 运行结果

- 每周五 09:15（Asia/Shanghai）由 GitHub 云端运行，本机无需开机。
- GitHub Actions 页面提供“Run workflow”按钮，可以随时手动执行，并选择 7/30/60/90 天窗口或填写精确 arXiv ID。
- 手动运行还可以勾选 `reset_state`，强制忽略旧数据库。
- `smoke_only=true` 时只做小成本真实能力验证：Qwen 必须按契约回复 `OK`；arXiv、Hugging Face、OpenAlex、GitHub 等单端点能力只发一个最小请求，Follow Builders、OpenReview、官方研究页、LessWrong/KOL RSS 最多按配置顺序 failover 5 个入口并在首个成功后停止；Tavily 只消耗一个 basic request，SMTP 只登录不发信。每项默认 30 秒总时限，不会调用生产召回器、生产解析器、SQLite 持久化回环、报告或邮件发送，因此绿色 Smoke 只是完整运行的前置条件。
- 自动与手动触发都执行 `python main.py`；只有完整研究闸门与报告质量闸门通过后才发送邮件。
- 研究检查点、报告渲染和邮件投递已经解耦。研究完成后先写入持久化 outbox；报告或 SMTP 失败不会撤销已完成研究，也不会把该报告登记成已成功交付。缺少当前输入契约的冻结快照，或完成内置修订后仍违反确定性交付质量契约的制品会立即隔离；网络、超时等其他渲染失败默认累计 3 次后隔离，并把候选原子退回 `pending_deep`。隔离记录不再阻塞后续研究。SMTP 失败仍保留已验证报告并让必需邮件任务失败。成功续投旧 outbox 后当前进程安全退出，云端在新状态 artifact 上传成功后自动排队第二个完整研究 run；若旧任务被隔离，当前进程直接继续新研究。若本轮新生成的报告被隔离，状态 artifact 仍会保存，但当前 Workflow 必须失败，不能把未完成交付显示为绿色；下一次运行也不会再被该坏任务阻塞。
- 报告若包含英文长段、评分表、字段拼装、绝对化“新范式”宣称、缺少关键人物/公开检索记录或缺少任一路线的一手链接，会先自动重写一次；仍不合格则任务失败且不发送邮件。人物名会先排除邮箱、URL、TeX/版式残片和拼接联系方式；OpenAlex 同名结果必须由当前论文题目交叉核验，只有具备最低背景、联系入口检索记录和直接身份锚点/当前工作对齐的人物才进入确定性索引。人物与原文索引由结构化证据确定性生成，不依赖模型抄写；全文每一个 HTTP(S) URL 都必须原样来自证据或人物档案，一个有效原文不能掩盖另一个猜测链接。
- 成功邮件除研究 Memo 外，还会附带本轮结构化筛选审计和运行日志；审计记录信源返回量、筛选理由及各阶段 token 用量，不保存 prompt、模型正文或私有推理。
- 去重数据库会在生产运行结束后以 `always()` 语义保存为私有 Actions artifact，包括报告/SMTP 失败后留下的研究检查点与 outbox。当前 schema v7 除 SQLite `quick_check`、表/字段迁移、领域 JSON 与 fingerprint/key 核验外，还会检查来源/执行检查点一致性，以及活动 outbox 是否满足当前人物/一手来源输入契约；不可再交付的旧任务在迁移时隔离并把候选退回深挖。证据在写库事务之前逐条执行与恢复路径一致的领域回环校验，结构不合法的记录按来源隔离并把本轮计入研究积压，健康同批记录仍会提交。最新 artifact 即使数据库页完整、但领域 payload 已损坏，也会被拒绝并继续尝试更早的不可变快照。恢复成功后会执行 `python main.py --inspect-state`，在日志中输出不含候选正文和 Secret 的队列计数、截断 key、失败类型及尝试次数。除非手动勾选 `reset_state=true`，所有快照均缺失、损坏或不兼容都会 fail closed，不会静默冷启动。
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

不要创建名为 `GITHUB_TOKEN` 的 Secret。GitHub 会为每次运行自动提供权限受限的临时 token，工作流已直接使用。工作流声明 `actions: write` 只用于在历史续投完成并成功保存状态后触发下一次 `workflow_dispatch`；代码仍是 `contents: read`，不会推送或改写仓库。

## 3. 配置 Actions Variables

同一页面切换到 `Variables`，按需添加：

| Variable | 填写内容 |
|---|---|
| `LLM_REQUEST_TIMEOUT_SECONDS` | 推荐 `180`；单次模型请求硬上限，业务层另有一次可审计重试 |
| `SMOKE_CHECK_TIMEOUT_SECONDS` | 推荐 `30`；每个真实接口冒烟的总时限，且冒烟不执行生产分页 |
| `OPENREVIEW_VENUES` | 逗号分隔的 venue id |
| `SEMANTIC_SCHOLAR_ENABLED` | 没有获批 Key 时填 `false`；只有同时创建 Key Secret 时才填 `true` |
| `RESEARCH_FEED_URLS` | 已验证的官方 RSS/Atom 地址，逗号分隔 |
| `LESSWRONG_SOURCE_ENABLED` | 可选；默认 `true`，使用公开官方 RSS，无需 Secret |
| `LESSWRONG_KARMA_THRESHOLD` | 可选；默认 `30`，只表示 frontpage Feed 的 karma 入选下限 |
| `ALIGNMENT_FORUM_SOURCE_ENABLED` | 可选；默认 `true`，使用公开官方 RSS |
| `KOL_SOURCE_ENABLED` | 可选；默认 `true`，读取代码内手工核验的个人 Feed |
| `KOL_CUSTOM_FEED_URLS` | 可选自定义 Feed；因身份未内置核验，默认只作二次解读 |
| `KOL_X_SOURCE_ENABLED` | 可选；默认 `true`，但只有配置 X Bearer Token 才读取内置 handle 的近期短文 |
| `RESEARCH_WATCHLIST_MODE` | 推荐 `merge`；自定义值追加到仓库内置目录，只有明确需要时才用 `replace` |
| `PRIORITY_RESEARCH_PAGES` | 可选的额外官方研究索引；留空使用内置海内外目录，追加页面不会自动成为权威背书 |
| `PRIORITY_RESEARCH_LINK_SAFETY_LIMIT` | 官方入口 HTTP 抓取熔断；默认 `0`，读取日期窗口内全部候选链接 |
| `PRIORITY_RESEARCH_CONCURRENCY` | 官方研究页并发数，默认 `6`，不建议超过 `12` |
| `ESTABLISHED_RESEARCH_ORGANIZATIONS` | 可选的额外已核验组织/具名实验室别名；禁止填整所大学和歧义短词 |
| `MONITORED_RESEARCH_ORGANIZATIONS` | 可选的额外监测组织；只增加关注，不因品牌自动放行 |
| `PRIORITY_RESEARCHERS` | 可选的额外重点研究者姓名；必须与公开主页或学术 ID 联合核验 |
| `REDDIT_API_ACCESS_APPROVED` | 只有收到 Reddit 批准且用途符合许可时填 `true`；否则保持空或 `false` |
| `REDDIT_USER_AGENT` | 例如 `python:ai-paradigm-radar:v1.0 (by /u/你的用户名)`；不含密钥，可放 Variable |
| `TAVILY_REQUEST_SAFETY_LIMIT` | Tavily credit 熔断；默认 `0`，搜索全部通过 Rubric 的深挖候选 |
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
| `PARADIGM_TECHNICAL_REPORT_MECHANISM_SLICE` | 推荐 `2`；单份 Technical Report 每次出队评估的机制数，总机制数不设上限并跨轮续跑 |
| `PARADIGM_DEEP_BATCH_SIZE` | 推荐 `1`；深挖检查点粒度，避免半完成档案入库 |
| `EMAIL_MAX_ATTACHMENT_BYTES` | 推荐 `10000000`；报告异常膨胀时阻断发送并保留 outbox |

不配置变量时，自定义研究 RSS 为空，但 LessWrong、Alignment Forum 与内置 KOL Feed 默认启用；研究入口、组织与重点研究者使用仓库中的版本化默认目录，不影响工作流语法。若仓库已有旧版 `PRIORITY_RESEARCH_PAGES` 或 `ESTABLISHED_RESEARCH_ORGANIZATIONS` 长名单，`merge` 会保留它们并同时加载新默认目录，不会冻结后续更新。

静态体检会直接拒绝拼错的布尔值、非整数、越界并发/超时值，以及格式错误的官方页面、Feed、Follow Builders 和 OpenReview venue，而不是沿用默认值继续运行。例如 `flase`、`three-thousand`、`PARADIGM_REPORT_ROUTE_CONCURRENCY=99` 或缺少 `https://` 的页面地址都会在访问状态 artifact 和真实 API 之前失败，并在告警邮件中列出变量名而不回显可能含凭据的完整 URL。

GitHub 上通常只需添加 `TAVILY_API_KEY`；`TAVILY_DISCOVERY_DOMAINS` 留空时同时发现社区和普通技术网页，结果仍只算索引线索。LessWrong/KOL RSS、Rubric、前沿覆盖地图和个人目录都随代码提交，无需创建 Secret 或 Variable。旧版 `PARADIGM_MAX_ANALYSIS_ITEMS`、`PARADIGM_MAX_DEEP_CANDIDATES` 等 Variable 可以删除；即使保留，新代码也不会读取。Reddit 的 Client ID/Secret 必须和“已批准”开关一起配置；只填密钥但不开启批准开关时，代码不会请求 Reddit API。Semantic Scholar 同理：Secret 留空、Variable 为 `false` 时，代码不会匿名请求。

跨提交恢复状态会从新到旧校验不可变快照，跳过下载失败、压缩包损坏、缺少数据库或无法迁移的快照；找到最近一份健康 SQLite 后再迁移到当前 schema 并读取前沿覆盖地图版本。它不再因为 commit SHA 或一个可迁移的版本号变化就重置。普通 Prompt、Skill 或报告样式更新会延续跨周历史；覆盖地图升级时仍恢复旧数据库用于证据去重，但程序会用 60 天窗口补扫新加入的技术面，报告时效仍使用普通任务窗口。已经分析过且正文未变化的材料不会再次调用 LLM。旧状态中缺少可靠分类来源的 Technical Report 会按当前文档元数据与系统结构规则重新核验；机制队列在高信号与普通材料之间按 1:3 加权轮转，两类内部再交错新增与 FIFO backlog。报告中每条已通过闸门的路线草稿也会按候选证据与写作 Skill 签名保存；总编超时后，下次只补未完成路线并重做轻量开篇，不重烧完整路线写作。存在活动 outbox 时先恢复它：成功交付后当前进程退出，工作流上传确认状态并自动 dispatch 一个 `reset_state=false`、`smoke_only=false` 的后续 run；确定性输入缺陷或耗尽渲染重试时隔离旧任务，并在当前进程继续新研究。自动 dispatch 失败会让当前任务失败并触发告警，不能静默把续投当成本周研究。若找不到任何健康 artifact，工作流会明确失败并要求人工判断；只有确定要建立新基线时才以 `reset_state=true` 运行。

分析续跑还包含三个细粒度边界。普通论文若已通过资格预筛但完整 Rubric 失败，下轮直接续跑完整 Rubric，不重复支付预筛调用。长报告的机制索引、已完成机制与失败次数由 schema v7 的 `origin_research_state` 持久化，父原点 `raw` 仅保留兼容视图；同版本旧快照不能减少已完成机制，增强写入不能推进或覆盖执行进度。候选快照与父报告进度逐原点在同一事务提交，慢请求/取消只延期未提交原点。深挖的技术综合闭合后立即保存经社区正文清洗的候选快照；人物轨迹阶段失败/超时若在 48 小时内重试，可只续跑人物阶段。来源版本、初筛输入、与当前路线相关的新增支持证据、指标跨量级变化、综合策略版本变化，以及超过 48 小时，均使该快照失效，重新综合或获取本期外部证据；模型 Rubric 未闭合不得缓存。相关支持线索即使在预算中断前尚未进入综合，也会随待续研候选保存，临时社区正文仍按原约束清除。外部增强到综合之间暂不缓存，因为社区正文不能长期持久化。Technical Report 分片内、人物内部和其余深挖子阶段仍需更细粒度检查点与生产回放验证。

## 4. 首次手动验收

1. 打开仓库的 `Actions`。
2. 选择 `AI 技术范式雷达`。
3. 点击 `Run workflow`，第一次选择 `7` 天，保持 `smoke_only=true`。精确 arXiv ID 留空，此时 `reset_state` 不影响结果。
4. 确认“配置体检”和“小成本真实接口冒烟”完成，并下载 `paradigm-radar-audit-*` 查看 `smoke_test_latest.json`。文件中的 `contract_version` 可确认线上使用的是哪版探针；`failure_kind=transient_availability` 表示第三方临时限流/超时，显示为 `degraded` 且 Workflow 可通过，但风险不会被隐藏；`multi_endpoint_unavailable` 表示某个多入口能力的有界样本全部失败；配置、真实鉴权或响应契约失败才显示为 `failed`。Smoke 不覆盖生产解析器与 SQLite 回环，必须继续执行下一步完整运行。
5. 再次点击 `Run workflow`，把 `smoke_only` 改成 `false`。已有历史状态或正在修复失败任务时保持 `reset_state=false`，让 schema v7 迁移和 outbox 恢复接管；只有新仓库完全没有可用 artifact、或人工确认要建立全新基线时才使用 `reset_state=true`。
6. 确认“运行离线回归测试”“抓取、分析并发送邮件”“保存跨周去重状态”全部为绿色。
7. 确认收件箱收到邮件及运行审计附件，并在该次运行的 Artifacts 中看到报告、`paradigm-radar-state-<run_id>` 与 `paradigm-radar-audit-*`。状态按运行保存为不可变快照；下一次任务恢复最近一份未过期快照，同时兼容旧的 `paradigm-radar-state` 名称。

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
- 工作流使用 Node 24 版本的 `checkout@v6`、`setup-python@v6` 与 `upload-artifact@v7`；任务不执行 git push，因此 checkout 不持久化临时凭据。
