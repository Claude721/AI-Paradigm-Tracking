# AI 技术范式雷达

这个项目不再以 GitHub 项目或 Product Hunt 产品为基本单位，而是每周捕捉正在形成的 AI 技术范式，并从技术反向锁定关键研究者。

目标只有一个：识别一个同时具备**清晰机制、扎实证据、足够外延空间和现实传播势能**的新范式。纯 benchmark 提分、狭窄组件替换、微小效率优化不会因为作者把问题讲得宏大就进入报告；团队背景无法核验、又没有独立讨论或承接的工作会先留在观察池。

## 新的工作流

```text
论文 / Technical Report / 官方研究博客
          ↓
 版本化前沿覆盖地图（具身 / World Model / AI4S / 系统等）
          ↓
 抽取一个或多个“新机制假说”
          ↓
 版本化离散 Rubric 确定性初筛
          ↓
 arXiv 正文/项目页补水 × 发布团队背景 × 外部响应
          ↓
 OpenAlex / S2 / ORCID / 个人主页核验关键人物
          ↓
 从低分辨率运行图递进构建技术心智模型
          ↓
 研究结果持久化 → 研究总编辑生成 Memo → durable outbox → 邮件交付
```

信源被分成三类，职责不能混用：

| 层级 | 信源 | 用途 |
|---|---|---|
| 原始发现 | arXiv、OpenAlex、OpenReview、官方 Technical Report/研究博客、LessWrong/Alignment Forum、手工 KOL 博客、官方 GitHub 发布事件 | 发现论文、原创思想、原生代码机制与技术谱系；正式 Technical Report 可拆出多个独立机制，概念文章和仓库只先生成机制假说 |
| 扎实度验证 | OpenReview 公开评审、Semantic Scholar 引用/作者图谱 | 验证实验、学术承接和人物轨迹 |
| 扩散验证 | Hugging Face Daily Papers、GitHub、Hacker News、Tavily、Reddit、X 标题搜索、Follow Builders、KOL 二次解读 | 验证讨论、实现、复现与二次传播；Tavily 只发现线索，作者本人发帖只核验身份，原帖不重复计算承接 |

Product Hunt、普通 GitHub Trending 和产品热榜不再决定候选，只在未来需要观察技术落地时作为弱信号。

## 如何判断“新范式”

系统先区分作者写下的宏大问题与论文真正做出的 intervention。模型不再凭整体印象直接输出“新颖性 7 分”，而是根据版本化 Rubric 回答带证据的二分/选择题；架构、算法、学习范式、数据、推理、Agent 闭环、具身、World Model、AI4S、系统与评测分别使用不同问题。程序按选项规则确定性计算初筛和最终决策：

- 已核验前沿组织发布的正式 Technical Report 优先进入解读，并逐项检查其中可独立承接的新机制。
- 大型 Technical Report 采用“两阶段抽取”：先建立紧凑机制索引，再逐机制回答类型 Rubric；单项格式失败不会清空整份报告，失败项保留到下轮重试。
- 普通论文不能只凭一个机构署名晋级；但多位长期前沿研究者的身份与方向被公开主页/学术 ID 核验后，本身构成发布势能，仍需先通过技术 Rubric。
- 发布者背景一般或无法核验时，需要跨来源的有内容讨论、独立复现、产品承接或高关注研究者的二次解读。
- 作者本人在 X 或个人主页发布工作可用于核验身份、机构与既往轨迹，但不能把自己的宣传转化为“社区已经验证”。
- Tavily 免费搜索用于发现公开索引的社区页面和独立技术博客；它不是平台全量 API，也不参与声量计分。Reddit OAuth 只有在明确获批后才读取帖子、评论与互动指标。
- 前沿机构目录区分“主动抓取、已建立、监测、重点研究者”四层；完整名单与官方核验入口见 [前沿机构与研究者 Watchlist](docs/FRONTIER_RESEARCH_WATCHLIST.md)。

Rubric 位于 [`rubrics/paradigm_rubric.json`](rubrics/paradigm_rubric.json)，可长期增删问题、调整 option 权重与阶段阈值。研究池与最终去留不固定取前 100、30 或 16 个；Rubric 分数与回答只进入审计，不进入最终报告。云端单次运行另有墙上时间软预算：它只让任务在 GitHub 硬超时前分批收尾，未处理材料会保留到后续运行，不会被写成 Rubric 淘汰。机制执行队列在高信号材料与普通材料之间按 1:3 加权轮转，两类内部再交错本周新增与旧 backlog，避免大型报告和历史积压长期挤占普通论文；旧状态中缺少可靠类型来源的“Technical Report”会先按当前保守规则重新核验，不能沿用历史查询标签消耗高优先级预算。

每个并行发现源另有独立墙上时间上限。上游卡死、请求失败、返回错误结构与真实零命中会分别记为 `timed_out`、`query_failed` 和完成但零结果；前两者会把本轮标成覆盖未闭合，不能生成“本期没有新信号”的确定性结论。发现安全上限只约束尚未执行的传输车道，已经取得的跨源材料不会再被第二次切片后静默丢弃。外部接口显式返回的 null 会先规范化为领域默认值；每条证据在写入 SQLite 前还会执行与恢复路径相同的结构回环校验。仍不合法的记录只隔离自身并进入未完成账本，健康同批记录继续提交。

行业发现范围位于 [`taxonomy/frontier_landscape.json`](taxonomy/frontier_landscape.json)。发现层不是一张关键词表，而是并行运行领域术语、重点研究者完整姓名、正式文档类型、官方研究索引、官方 GitHub 组织的新发布事件、Hugging Face 策展和人工精确补录等独立车道。GitHub 车道只在仓库明确链接到外部一手论文或官方技术页时把该外部材料送入机制抽取；repository-only 事件只进入审计和实现证据。每次审计同时显示逐领域状态、每条召回车道和逐个官方入口健康度，避免“报告为零”掩盖某家公司页面解析失败。普通论文和学术聚合索引只扫本次任务窗口（默认 7 天）；正式 Technical Report、重点研究者和官方入口使用 30 天回补，空数据库或地图升级时仅这些高信号车道扩到 60 天。回补窗口只扩大召回，不改变交付时效：一手材料仍须落在普通任务窗口内才可称为本期新发布。历史漏项只用于定位通用失效模式；修复完成后，发布门验证抽象召回契约，不再重复追踪某篇历史论文。

## 每周交付物

报告文件位于 `reports/output/paradigm_radar_YYYY-MM-DD.md`。候选通过初筛后会调用 [`technical-mental-model`](skills/technical-mental-model/SKILL.md)：先选择一条能统摄技术的训练、推理、表示或行动流程，用二到四句建立低分辨率运行图，再沿真正改变理解的接口逐层提高分辨率并纠正错误直觉。研究总编辑据此写约 500 字的本期 Memo，并按共同 background 把多篇工作组织成技术路线。内部脚手架不会作为固定字段输出，完整方法见 [从低分辨率到高分辨率的技术心智模型写作法](docs/MENTAL_MODEL_WRITING_METHOD.md)。每条路线还必须用一小段 `讨论势能判断` 交付“克制结论—客观依据—覆盖边界”：说明非作者主体在哪个平台讨论了什么、是否出现复现或采用，以及当前为什么只能判断为单点提出、开始承接、多团队扩散或证据不足；不展示内部评分。正文必须用中文转述；论文名和必要术语可以保留英文，但英文摘要、长句和“真正意义上的新范式”等绝对化结论会触发自动重写。每份非空报告还会确定性生成“关键人物与公开联系入口”和“原文与一手资料”两个索引；人物只使用已核验的公开职业信息，原文链接直接来自候选证据。质量闸门不仅要求每条路线至少有一手 URL，还逐一核对全文所有 URL 是否原样存在于证据或人物档案，额外的“看似合理”链接同样会阻断发送。人物背景/联系方式检索未完成、任一路线缺少一手 URL 或遗漏势能判断都会把候选保留到后续补全。

每条重要路线都会追踪前三位作者、末位/资深作者和重点名单中的关键作者；官方项目页明确标注共同一作/通讯关系时按贡献角色组织，不能把大型合作论文写成“某位大佬的论文”。进入报告的人物名先通过语法安全检查，邮箱、URL、TeX/版式残片、数字和拼接联系方式不会被当成人名。OpenAlex 同名结果必须由当前论文题目交叉核验；只有具备最低背景、已完成公开联系入口检索，并有主页/ORCID/学术档案等直接身份锚点或当前工作对齐记录的人物才进入确定性索引。没有公开联系方式时，报告保留检索记录和能够确认的最低背景；无法确认身份的人物档案留在内部待补全，不能冒险交付。

系统使用独立的 `database/paradigm_radar.db`。同一证据按 DOI、arXiv ID 或稳定 URL 去重；观察池和已报告路线会在每周重新检索近期讨论。有界刷新按“最久未尝试”轮转，成功但无变化也会清除旧执行错误，避免同一批历史路线永久占据队首或尾部路线饿死。同一范式只有在证据签名发生实质变化时才会以“进展更新”再次出现，因此相邻周不会原样重复。报告前还会独立做时效核验：**首次进入本地数据库不等于本周新发布**；只有窗口内有可核验发布日期的一手材料，才能标为新路线。旧材料只有出现窗口内独立讨论、复现、采用或可核验指标增量时才作为进展更新；网页 `dateModified` 不能冒充发布日期。

研究检查点与报告交付是两个状态：研究完成后先把候选快照写入持久化 outbox；多路线报告再逐路线写作并逐条保存 checkpoint，最后只生成轻量的全局 Memo 框架，程序确定性装配全部路线。每个模型阶段都核对输入/输出身份与基数；漏回、外来对象或最终 Rubric 未闭合只会让对应对象回到 `pending`/`pending_deep`，批次异常会二分隔离，健康同批对象继续提交。

报告渲染采用有界重试：冻结快照缺少当前人物/一手来源输入契约，或完整制品在内置修订后仍违反确定性交付契约时，立即 `quarantined`；网络、超时等其他渲染失败默认累计 3 次后隔离。隔离记录保留在数据库供审计，其候选与证据在同一事务中退回 `pending_deep`，不再永久占住 outbox 队首。SMTP 故障与渲染故障分账：已验证报告继续留在 outbox，开启 `EMAIL_PUSH_REQUIRED` 时本轮仍明确失败，绝不提前登记已投递。成功恢复历史交付后，当前进程退出并由 GitHub Actions 在状态上传后排队完整研究 run；若恢复任务被隔离，则当前进程直接继续新研究。若本轮新生成的报告被隔离，状态会先安全保存，但当前 Workflow 仍失败，因为本轮没有完成报告/邮件交付；下次运行不会再被该坏任务卡住。SMTP 接受邮件后若进程在数据库确认前硬退出，系统按同一 `Message-ID` 至少一次重试，无法承诺所有邮箱服务商上的绝对 exactly-once。所有计划材料均完成判断后，如果没有候选跨过联合门槛，会正常发送一份空雷达。若仍有待分析材料、待深挖路线或执行失败，邮件标为 `[阶段性研究]` 并在 Memo 开头公开已完成/计划量；若研究已经闭合、只是 OpenReview 或某个动态官方入口覆盖失败，则标为 `[覆盖受限]`，二者都不能冒充确定性的零创新结论。

## 配置与运行

环境要求为 Python 3.11+。安装依赖后复制配置模板：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

至少填写 DashScope Key 与 OpenAlex 免费 Key。Semantic Scholar Key 是可选增强项；没有学术邮箱时保持 `SEMANTIC_SCHOLAR_ENABLED=false` 且 Key 留空，程序会完整跳过，不会匿名调用。人物轨迹仍会通过 OpenAlex、ORCID 与已核验个人主页补齐。

LessWrong、Alignment Forum 与内置 KOL 博客全部使用公开 RSS/Atom，默认开启且不需要 Secret。目录位于 `research_watchlist.py`：有稳定 Feed 的作者会自动读取；无 Feed 的作者仍保留个人主页与可选 X handle 供身份核验。`TWITTER_BEARER_TOKEN` 未配置时不会请求 X，不影响个人博客召回。

```bash
python main.py             # 立即执行一次
python main.py --schedule  # 每周五按配置持续运行
python main.py --report    # 优先续投 outbox；否则不重新抓取，调用总编辑重建最近报告并发信
python main.py --status    # 查看模型配置
python main.py --inspect-state # 只读查看数据库与 outbox 健康度
python main.py --doctor    # 零网络检查配置是否齐全
python main.py --smoke-test # 小成本真实检查接口；SMTP 只登录、不发邮件
```

第一次完整运行前先执行 `python main.py --smoke-test`。它不会运行流水线、不会创建报告或修改范式数据库，结果保存在 `logs/smoke_test_latest.json`，且不记录密钥和响应正文。Smoke 使用与生产召回器隔离的最小能力探针：arXiv、OpenAlex、GitHub 等单端点接口只发一个请求；官方页面、OpenReview venue、LessWrong/KOL RSS 和 Follow Builders 这类多入口能力按配置顺序最多尝试 5 个，首个响应契约成功即停止，避免单站边缘防护被放大成系统故障。它不会运行领域/人物/报告车道、生产解析器、详情页抓取、分页或 SQLite 持久化回环；绿色 Smoke 是完整运行的前置条件，不是生产流水线已经通过的证明。鉴权失败和响应结构变化会返回非零；公共服务的 429、5xx、网络错误或超时会标为带 `failure_kind` 的 `degraded`，保留风险但不把一次第三方抖动误判成代码不可部署。辅助源失败也会显式降级。

完整运行还会生成 `logs/run_audit_latest.md`、`logs/run_audit_latest.json` 和 `logs/current_run.log`。其中包含信源返回量、漏斗、每条材料/路线的结构化去留理由，以及各阶段模型 token 用量；不会保存 prompt、模型回答正文或模型私有推理。信源部分失败、全部失败、超时和真实零命中会分别记账；机制抽取后先保存可续跑候选，再把原文标为已分析，避免中途异常造成永久漏项。启用邮件后，Markdown 审计和本轮日志会随报告一起发送。研究进度与外部覆盖是两条独立状态轴：待分析/待深挖/执行失败时标题为 `[阶段性研究]`，正文披露已完成范围；研究已闭合但可选信源或动态页面覆盖不全时标题为 `[覆盖受限]`。只有两条轴都闭合，0 条交付才可解释为本期没有合格新信号。

定时参数：

```env
SOURCING_LOOKBACK_DAYS=7
PARADIGM_RECALL_OVERLAP_DAYS=30
PARADIGM_PRIORITY_AUTHOR_SWEEP_ENABLED=true
PARADIGM_BOOTSTRAP_LOOKBACK_DAYS=60
PARADIGM_RESEARCHER_PROFILE_LIMIT=6
PARADIGM_KEY_RESEARCHER_LIMIT=3
PARADIGM_RUN_BUDGET_SECONDS=3600
PARADIGM_STAGE_RESERVE_SECONDS=1200
PARADIGM_DISCOVERY_SOURCE_TIMEOUT_SECONDS=600
PARADIGM_REPORT_TIMEOUT_SECONDS=1200
PARADIGM_REPORT_REQUEST_TIMEOUT_SECONDS=360
PARADIGM_REPORT_ROUTE_CONCURRENCY=2
PARADIGM_REPORT_MAX_RENDER_ATTEMPTS=3
SCHEDULE_DAY_OF_WEEK=fri
SCHEDULE_HOUR=9
SCHEDULE_MINUTE=15
SCHEDULE_TIMEZONE=Asia/Shanghai
```

如需把交付和普通发现范围改为最近一个月，将 `SOURCING_LOOKBACK_DAYS` 改为 `30`。默认周报的普通召回只扫 7 天；`PARADIGM_RECALL_OVERLAP_DAYS=30` 只给正式报告、重点研究者和官方入口回补索引晚到、发布页后补完整报告和单车道短期失败。这既保留重要节点的容错，也不会在 reset 后把所有宽关键词放大到 60 天。

## 邮件推送

```env
EMAIL_PUSH_ENABLED=true
EMAIL_PUSH_REQUIRED=true
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USERNAME=your-email@qq.com
SMTP_PASSWORD=邮箱生成的SMTP授权码
SMTP_FROM=your-email@qq.com
SMTP_TO=recipient@example.com
SMTP_USE_SSL=true
SMTP_USE_STARTTLS=false
EMAIL_MAX_ATTACHMENT_BYTES=10000000
```

邮件主题会显示“新范式”和“进展更新”数量，完整 Markdown 作为附件发送。没有合格候选且研究判断、核心覆盖都已完成时会发送空雷达；软预算积压使用 `[阶段性研究]`，纯外部覆盖缺口使用 `[覆盖受限]`。普通步骤失败时，云端另有独立失败提醒，不依赖报告文件已经生成；GitHub 直接取消整个 job 或达到硬超时时无法保证后置提醒仍被执行，因此仍需依赖软时间预算主动收尾。

手动执行 `python main.py` 与每周调度使用同一个流水线，都会在报告生成后发送邮件。开启
`EMAIL_PUSH_REQUIRED=true` 后，SMTP 失败会让任务明确失败，且不会登记为“已交付”。

## 云端自动运行

仓库已包含 GitHub Actions 工作流 `.github/workflows/weekly-radar.yml`：每周五
09:15（Asia/Shanghai）自动运行，也可以在 GitHub 的 Actions 页面手动运行。云端运行不依赖
本机开机，并会跨运行恢复去重数据库。完整上线步骤见
[`docs/CLOUD_AUTOMATION.md`](docs/CLOUD_AUTOMATION.md)。

## 主要代码

```text
paradigms/
  landscape.py       版本化产业/技术覆盖地图与运行审计
  models.py          证据、范式、研究者模型
  discovery.py       论文/研究博客优先发现
  analyzer.py        新机制抽取与人物轨迹分析
  clustering.py      论文级结果聚合为范式
  enrichment.py      引用、实现、讨论、人物增强
  rubric.py          版本化量表校验、确定性计分与客观证据题
  scoring.py         汇总 Rubric 与发布者/社区结构化证据
sources/
  arxiv_source.py
  arxiv_document_source.py
  openalex_source.py
  openreview_source.py
  semantic_scholar_source.py
  researcher_profile_source.py
  paradigm_evidence_source.py
  social_web_search_source.py
  reddit_evidence_source.py
  priority_research_source.py
database/paradigm_store.py
reports/paradigm_generator.py
agents/paradigm_orchestrator.py
skills/weekly_research_memo/SKILL.md
skills/weekly_memo_revision/SKILL.md
```

旧版项目型流水线仍保留，可通过 `PIPELINE_MODE=legacy` 回退，但默认不再使用。
