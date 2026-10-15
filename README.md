# AI 技术范式雷达

一个在 GitHub Actions 上定时运行的 AI 技术研究项目：广泛获取一手信息，提取机制假说，查证外部承接与关键人物，最后交付有证据支持的中文研究 memo。

它不是论文日报，也不保证每周出现新范式。官网反爬、单个 API 故障只是广度缺口，不应拖住其他健康材料；但技术真实性、人物归因和报告质量不能因此降低。

当前双层模型已通过真实接口冒烟，3 天单材料研究已完成观察判断；非空 memo 质量及整周交付仍须验收，不能只凭 Actions 绿色称为稳定 V0。具体证据见[工程状态](docs/PROJECT_HEALTH_REPORT.md)。

## 1. 从信息到结论

```text
HTTP / API / RSS 获取材料
    ↓
校验原始发布时间、稳定链接与来源身份，合并去重
    ↓
按来源和发布者分散选定本期有界研究计划
    ↓
SUB：资格初筛、机制提取、正式报告机制拆分
    ↓
获取正文、独立讨论/实现/采用证据，核验关键人物
    ↓
MAIN：路线综合、人物轨迹、中文起草与总编
    ↓
确定性校验 Rubric、人物、时效、全文链接及编辑质量
    ↓
保存报告待投递记录 → 邮件确认 → 登记已交付签名
```

| 阶段 | 要解决的问题 | 不做什么 |
|---|---|---|
| 发现 | 本期有哪些值得研究的原始技术材料？ | 不把搜索命中、平台热度直接当作技术结论 |
| 初筛 | 新机制改变了哪个对象、信号或运行接口？ | 普通论文不自动等于范式；命名和品牌不能绕过门槛 |
| 查证 | 原文是否支持机制？是否出现独立讨论、复现或采用？ | 作者自发帖、聚合列表和 star/fork 不冒充独立承接 |
| 综合 | 多份材料是否属于同一条路线？能力与限制是什么？ | 不混合不同模型/环境的实验，不把预测写成实测 |
| 人物 | 谁实际推进技术？背景与公开职业联系入口能否核验？ | 不猜邮箱、任职，不合并同名研究者 |
| 交付 | 是否能给出完整、中文、可追溯的研究结论？ | 不输出内部分数、英文摘要或阶段性 memo |

一手发现包括 arXiv、OpenAlex、OpenReview、官方研究博客/目录、原创思想文章和已核验官方原生实现。HN、HF、GitHub Search、KOL 二次解读、Follow Builders、Tavily 等补充线索必须继续核验。普通论文通常提出一个机制假说；正式 Technical Report 可以拆出多个独立机制。来源职责见[信源说明](信源说明.md)。

官网 HTML 使用正常 HTTP 与解析器读取；部分登记站点有公开 JSON 目录适配。不需要 Computer Use、浏览器登录、验证码绕过或付费墙解锁。发布日期不能用首次入库、平台收录或 `dateModified` 替代。

## 2. 什么结果可以交付

默认普通窗口为 7 天，高信号回补为 14 天；按发布者/来源轮转选定最多 48 个原点、刷新最多 4 条历史路线。先完成本期原点初筛，去重后所有通过技术门槛的候选必须全部深挖，不设深挖 Top-K，也不由模型选择做几条。取材有界，不是全网覆盖声明；未选原始材料不等于进入深挖的候选。

| 本轮结果 | 处理 |
|---|---|
| 本期原点、全部深挖、选定历史刷新判断闭合，且有通过质量门槛的路线 | 交付完整结论；物理入口/未选原始材料/其他历史积压的缺口留在审计 |
| 无合格路线，且本期选定判断全部完成 | 空雷达：只说明已研究材料中没有合格路线，不断言全网没有创新 |
| 有效一手输入不足，或本期选定判断有未完成项（即使已有合格路线） | 明确失败并保存健康检查点，不截取部分结果、不发阶段性 memo |
| 报告质量或必需邮件投递失败 | 明确失败，已完成研究不回滚，不提前登记已交付 |

至少 2 类一手发现通道可用才具备交付基础；这不是“两个网站都谈论过”的趋势认证。技术去留由[版本化 Rubric](rubrics/paradigm_rubric.json)、发布者和外部承接共同决定。报告按共享问题与能力边界组织路线，不逐篇填表；每条路线给出可核验的讨论势能判断。

每条入选路线须有核验过的一手 URL，报告中每个 HTTP(S) URL 都原样来自证据或已核验人物档案。猜测链接、临时签名 URL、未核验关键人物和不合格中文写作会阻断交付。

## 3. 最小 GitHub 配置

在仓库 **Settings → Secrets and variables → Actions** 配置。无需把 `.env.example` 的每一行都建成 GitHub Variable；未设置的可选参数使用代码/工作流默认值。

### 两层模型各自独立

| 名称 | 放在哪里 | 含义 |
|---|---|---|
| `SUB_AGENT_PROVIDER` | Variables | 初筛 API 服务商标签，例如 `dashscope` |
| `SUB_AGENT_MODEL` | Variables | 初筛/机制提取的实际模型 ID |
| `SUB_AGENT_BASE_URL` | Variables | SUB 接口根地址，如提供方给出的兼容 `/v1` 地址 |
| `SUB_AGENT_LLM_API_KEY` | Secrets | 与 SUB 地址和账户匹配的 Key |
| `MAIN_AGENT_PROVIDER` | Variables | 深挖 API 服务商标签，可与 SUB 不同 |
| `MAIN_AGENT_MODEL` | Variables | 综合判断、人物分析、报告写作的实际模型 ID |
| `MAIN_AGENT_BASE_URL` | Variables | MAIN 接口根地址 |
| `MAIN_AGENT_LLM_API_KEY` | Secrets | 与 MAIN 地址和账户匹配的 Key |

两套配置不继承全局、不互相借用、不猜测厂商默认模型；缺项会在模型调用前明确报错。可以使用同一模型和同一 Key，也可以用便宜的初筛模型加更强的研究模型。换某层时一起核对该层提供方、模型、地址和 Key，不用修改代码或重置数据库。

模型不限 Qwen，但接口须兼容 Chat Completions 并返回完整 JSON；不直接支持厂商原生 Messages/其他不兼容协议。`BASE_URL` 不填 `/chat/completions` 完整路径，也不夹带密钥。请求策略不绑定模型名、固定地址或日期：`dashscope` 选择显式思考模式与 `max_completion_tokens`，其他提供方默认标准 `max_tokens`。特殊接口只需设置该层可选 `SUB_AGENT_REQUEST_OPTIONS` / `MAIN_AGENT_REQUEST_OPTIONS` JSON，覆盖输出参数、额外请求体或不支持的参数，无需改研究代码。用法见[配置清单](docs/CONFIGURATION_CHECKLIST.md)。旧全局 `LLM_PROVIDER`、`LLM_MODEL`、`LLM_BASE_URL`、`LLM_API_KEY`、`DASHSCOPE_API_KEY` 以及旧角色 `*_API_KEY` 已不再读取。

### 信源与邮件

| 名称 | 放在哪里 | 是否需要 |
|---|---|---|
| `OPENALEX_API_KEY` | Secrets | 推荐，增强论文召回、机构及人物身份；未配置时降级 |
| `TAVILY_API_KEY` | Secrets | 可选，用于公开网页索引线索；使用前核对账户费用与限额 |
| `SMTP_USERNAME` | Secrets | 云端邮件必需，发件账号 |
| `SMTP_PASSWORD` | Secrets | 云端邮件必需，邮箱应用密码/SMTP 授权码，不是登录密码 |
| `SMTP_TO` | Secrets | 云端邮件必需，收件地址，多个地址用英文逗号分隔 |
| `GITHUB_TOKEN` | 自动注入 | 工作流使用 `github.token`，不需要另建同名 Secret |

云端正式流程目前固定 QQ SMTP、465/SSL，启用并要求邮件交付；Smoke/信源检查关闭邮件。现有发信路径不改变。X、未获批 Reddit、未获批 Semantic Scholar 默认关闭，不需要为了 V0 申请这些 Key。OpenReview、Feed、名单和其他平台配置见[完整清单](docs/CONFIGURATION_CHECKLIST.md)。

## 4. 常用参数到底控制什么

下面是默认值，不是要求你全部显式配置。GitHub 中已经设置的 Variables 会覆盖默认值。

| 参数 | 默认 | 含义 |
|---|---:|---|
| `PARADIGM_RECALL_OVERLAP_DAYS` | 14 | 高信号材料重叠回补；有效值不小于普通窗口 |
| `PARADIGM_BOOTSTRAP_LOOKBACK_DAYS` | 14 | 冷启动高信号回补；有效值不小于重叠窗口 |
| `PARADIGM_RESEARCH_ORIGIN_LIMIT` | 48 | 本期跨来源选定的研究原点数 |
| `PARADIGM_RESEARCH_REFRESH_LIMIT` | 4 | 本期检查的历史路线数；0 关闭刷新 |
| `PARADIGM_MIN_PRIMARY_SOURCES` | 2 | 至少可用的一手发现通道类别数 |
| `PARADIGM_SOURCE_QUERY_LIMIT` | 100 | arXiv 每个查询的最大召回量 |
| `PARADIGM_SOURCE_PAGE_LIMIT` | 2 | OpenAlex/OpenReview/已支持目录每查询分页上限；未穷尽保留覆盖缺口 |
| `PARADIGM_OFFICIAL_DETAIL_LIMIT` | 6 | 每个官方入口本轮新增详情尝试数 |
| `PARADIGM_ALLOW_UPDATES` | true | 旧路线有本期新增实质证据时允许进展更新 |
| `PARADIGM_RUN_BUDGET_SECONDS` | 3600 | 研究软时间预算，不是人民币费用上限 |
| `PARADIGM_REPORT_TIMEOUT_SECONDS` | 1200 | 整份报告渲染时间上限 |
| `LLM_REQUEST_TIMEOUT_SECONDS` | 180 | 两层共享的分析请求超时，不是共享模型配置 |
| `TAVILY_REQUEST_SAFETY_LIMIT` | 0 | 单进程搜索请求熔断；0 不设额外次数上限 |
| `TAVILY_MONTHLY_CREDIT_LIMIT` | 1000 | 跨运行的 UTC 月度 credit 保护；0 明确关闭保护 |
| `PARADIGM_AUTO_RESUME_ENABLED` | false | 是否允许健康状态保存后的同批纯预算续跑，开启会增加调用 |

普通窗口由手动 `lookback_days` 输入控制，定时默认 7 天；不是仓库 Variable `SOURCING_LOOKBACK_DAYS`。本地 CLI 才直接读该环境变量。冷启动和重叠窗口是不同阶段，不要为了减小配置表随意合并。

旧 `PARADIGM_RESEARCH_DEEP_LIMIT` / `PARADIGM_DEEP_SAFETY_LIMIT` 不再映射到正式 Workflow，也不能截断 v3 深挖队列；仓库中若仍有同名 Variables 可删除，无须填写替代项。时间/额度异常仍如实保留 pending 并阻断交付，不允许为“全量完成”伪造结论。

`TAVILY_SOCIAL_SEARCH_ENABLED=false` 关闭 Tavily；`TAVILY_DISCOVERY_DOMAINS` 限制正式搜索域名，留空不限制。`TAVILY_SOCIAL_SEARCH_DOMAINS` 只供 Smoke 样本，两者不是重复字段。未知用量不搜索/不退款，账本不能限制同账户其他客户端，也不能替代供应商 PAYG/账户硬限额。

Tavily 使用工作名的宽召回再核验关联；逐候选回执区分 API 原始返回数、过滤后保留数、已确认 credits 与 request_id。过滤后 0 条不等于没有调用，也不等于全网没有讨论。无关仓库不能给当前路线借用 stars、发布者身份或实现证据。

并发、批次、额外安全熔断、Rubric 路径及名单追加属于高级配置，通常无需调整。`LLM_ENABLE_THINKING` 是 DashScope 协议的默认模式，不按模型 ID 或 URL 判定；不是对所有厂商的通用开关，角色请求选项可独立覆盖。默认值与作用范围详见[配置清单](docs/CONFIGURATION_CHECKLIST.md)。

## 5. 工作流如何使用

主工作流为 [weekly-radar.yml](.github/workflows/weekly-radar.yml)，定时设置是每周五 09:15（Asia/Shanghai），Ubuntu / Python 3.12，job 上限 90 分钟。手动 Run workflow 默认 `smoke_only=true`，不会直接启动正式研究。

| 模式/输入 | 作用与边界 |
|---|---|
| `source_check_only=true` | 优先于 Smoke；检查公开/已配置一手入口，无模型、邮件或生产状态恢复 |
| `source_platform_checks=false` | 信源检查默认不访问付费/需批准的补充平台；显式开启仍需相应授权 |
| `smoke_only=true` | 最小模型 JSON 与输入能力检查，不调用 Tavily/X/Reddit/Semantic Scholar/SMTP |
| 两个 only 均为 false | 正式有界研究、中文报告与必需邮件，会消耗模型/API 费用 |
| `lookback_days` | 普通窗口支持 3/7/30/60/90 天；3 天正式运行不等于低费用样本 |
| `reset_state=false` | 正常恢复 V0 状态；true 为显式从空状态重建，不是常规重试选项 |
| `seed_arxiv_ids` | 临时精确材料可见性，不绕过技术和承接门槛 |
| `resume_campaign_id` | 只恢复指定固定计划，不启动新发现；须关闭 Smoke，不能重置 |

独立 [one-route-acceptance.yml](.github/workflows/one-route-acceptance.yml) 是无邮件、无生产状态的单路线真实样本，默认 3 天、两层合计最多 20 元。`models_only=true` 可先只验两层模型。价格由 `price_contracts` 输入的两角色 JSON 契约提供，不在代码里绑定厂商、模型、地址或日历；换模型只需更新已核验价格数据，不能套用旧价格。单路线可显式允许最多 8 次 OpenAlex 身份查询，其他收费平台仍隔离。见[样本指南](docs/ONE_ROUTE_ACCEPTANCE.md)。

信源子集接口检查与更多操作说明见[云端指南](docs/CLOUD_AUTOMATION.md)。不要把一次 Smoke、一个入口 passed 或 Workflow success 当成研究质量/完整生产验收。

## 6. 状态、失败与制品

研究结果保存到 SQLite，报告进入独立的待投递记录（outbox）。报告或 SMTP 失败可恢复已完成研究；只有确认交付后才更新 `last_reported_signature`。SMTP 接受与本地确认之间仍有至少一次重试边界，不承诺跨邮箱的绝对 exactly-once。

同窗续跑复用选定材料、窗口和快照。正常新窗口会把旧未交付研究计划标为 superseded，旧 pending 不冒充成功，也不锁住新一周。合规待投递 outbox 优先恢复；恢复后先保存状态，再独立排队本周研究，不在同一 90 分钟进程叠加。

| 位置/制品 | 内容 |
|---|---|
| `database/paradigm_radar.db` | 研究状态、候选、去重、交付记录与 provider credit 账本 |
| `reports/output/` | 本地生成的 Markdown 研究报告 |
| `logs/` | 去敏运行审计、用量、离线/来源诊断 |
| `paradigm-radar-v0-state-<run_id>` | 云端跨周数据库及状态元数据快照 |
| `paradigm-radar-audit-<run_id>` | 云端审计、配置诊断和执行代码身份 |
| `.private/research_archive/` | 本地审阅 JSON：含淘汰/留存样本、实际提示词/可见回答及独立人工反馈；不含密钥 |
| 邮件 `research-reviews-<run_id>-<attempt>.zip` | 正式 Workflow 单独发出的本轮审阅数据附件，不上传新增的审阅数据 GitHub 制品 |

JSON 留存默认开启，不改变研究逻辑、不新增模型调用、不接入训练，**不需要归档 Secret**。Runner 本地收集后用现有 SMTP/收件人独立寄出普通 ZIP；它不是阶段性研究报告，失败时也尝试寄出已捕获样本。请下载到电脑，用本地 `merge` 累计保存，已有 `feedback.json` 人工标签不被新附件覆盖，原始模型判断不改写。Runner 不会读取你电脑的人工标签；原有业务状态/报告/去敏审计制品保持不变。ZIP 未加密，请私下保存。操作见[本地留存与人工反馈](docs/RESEARCH_ARCHIVE.md)。

云端首次 V0 没有新 namespace 快照时从空库启动，不导入旧失败队列，旧制品不删除。后续恢复健康 V0 快照；已有快照都不可用时明确失败，不静默清库。不要提交 `.env`、数据库、日志或生成报告。

## 7. 本地运行与验证

Python 3.11+：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

先填写两层模型；本地不需要发信时保持 `EMAIL_PUSH_ENABLED=false`。

```bash
python main.py --doctor          # 静态配置检查，不联网
python main.py --status          # 查看配置，密钥完全遮蔽
python scripts/offline_checks.py # 清除生产配置、拒绝网络的发布门
python scripts/source_audit.py --list  # 无网络列输入清单，故意不读 .env
```

以下操作需明确授权真实访问、费用或邮件：

```bash
python main.py --smoke-test --smoke-skip-smtp --smoke-skip-tavily
python main.py                   # 正式研究；启用邮件时发送
python main.py --resume-research # 恢复原活动研究计划
python main.py --report          # 续投 outbox 或重建报告，可能调用 MAIN/发送邮件
```

`--inspect-state` 不请求网络，但旧库初始化可能执行兼容迁移，不是数据库只读审计。`--schedule` 启动本地调度；`SCHEDULE_*` 只控制本地调度/日期，不改变 GitHub cron。选择云端调度时无需本机开机，不要同时开启两套调度。

## 8. 代码与详细文档

| 入口 | 职责 |
|---|---|
| [main.py](main.py) | CLI、研究/报告交付入口 |
| [agents/paradigm_orchestrator.py](agents/paradigm_orchestrator.py) | 本期选定计划、预算调度与对象故障隔离 |
| [paradigms/discovery.py](paradigms/discovery.py)、[sources/](sources/) | 来源访问、解析与发现 |
| [agents/llm_utils.py](agents/llm_utils.py) | MAIN/SUB 独立配置、共享兼容客户端与请求策略 |
| [paradigms/analyzer.py](paradigms/analyzer.py)、[rubrics/](rubrics/) | 机制抽取、路线/人物判断与版本化门槛 |
| [database/](database/) | 检查点、范围回执、待投递报告与额度账本 |
| [reports/](reports/)、[notifications/](notifications/) | 中文编辑、确定性索引和邮件 |

深入阅读：[设计原理](docs/PARADIGM_RADAR_DESIGN.md)、[配置清单与清理指南](docs/CONFIGURATION_CHECKLIST.md)、[信源说明](信源说明.md)、[技术心智模型](docs/MENTAL_MODEL_WRITING_METHOD.md)、[来源诊断](docs/SOURCE_ACCEPTANCE.md)、[工程验收状态](docs/PROJECT_HEALTH_REPORT.md)。

`PIPELINE_MODE=legacy` 的项目型筛选、Product Hunt、WeChat、QuantFilter 和自建 Feed 只保留兼容，不属于默认 V0 研究路径或云端可切换模式；无需为 V0 配置它们。`docs/RUN_*` 等历史记录保留当时观测，不是当前配置教程。
