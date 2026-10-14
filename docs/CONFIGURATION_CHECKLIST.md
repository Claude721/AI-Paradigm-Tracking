# V0 配置清单

生产密钥仅放本地 .env 或 GitHub Actions Secrets，不写进聊天、Variables、日志或 Git。模板为 [.env.example](../.env.example)。

本清单描述默认 paradigm/V0 路径，不要求把全部默认参数复制到 GitHub Variables。模型六项字段及云端投递所需 Secrets 配好即可；其余只有需要覆盖默认行为时才显式设置。`config.py` 中 legacy 的 Product Hunt、WeChat、QuantFilter、自建 Feed 参数仍有代码消费者，但不属于 V0 配置，不能把“当前路径不用”误写为全项目死代码。

## 配置放在哪里、怎样才生效

| 配置面 | 生效规则 |
|---|---|
| GitHub Variables / Secrets | 必须由 `.github/workflows/weekly-radar.yml` 显式注入；仅在设置页建名称不会自动成为进程环境变量 |
| 本地 `.env` | `config.py` 读取；显式进程环境优先于 `.env`；来源诊断脚本故意忽略 `.env` |
| Workflow inputs | `lookback_days`、`seed_arxiv_ids`、`reset_state` 等只对当前手动运行生效，不是 Repository Variables |
| 本地调度/自定义路径 | `SCHEDULE_*`、`PARADIGM_RUBRIC_PATH`、`FRONTIER_LANDSCAPE_PATH` 不在云端 Variables 映射中；GitHub cron 与默认仓库路径由工作流/代码管理 |
| 云端邮件策略 | 当前固定 QQ 465/SSL，正式运行 `EMAIL_PUSH_ENABLED/REQUIRED=true`；同名 Repository Variables 不会改变这个固定策略 |

共享的 `LLM_REQUEST_TIMEOUT_SECONDS` 与 `LLM_ENABLE_THINKING` 是请求策略，不是全局模型/Key。后者只对已核验官方 Qwen 3.7 Plus 北京组合生效，不保证任意模型关闭思考。`FOLLOW_BUILDERS_ENABLED/FEED_URL`、`TAVILY_SOCIAL_MAX_RESULTS`、`PARADIGM_ALLOW_UPDATES` 与 `LOG_LEVEL` 已映射到云端，默认不需要建这些 Variables。

## 最小配置

- PIPELINE_MODE=paradigm。
- MAIN/SUB 分别配置能完成 Chat Completions / JSON 的模型 API，各自模型名、地址和 Key 对应；可以使用同一模型和同一 Key。
- 默认公开来源保持开启；运行时至少 2 个一手发现通道可用。OpenAlex 与 GitHub 推荐，但不是全部信源都必须配置/成功。
- 如需邮件，SMTP 配置完整；已有发信功能不需要重新设计。
- 自动续跑、X、未获批 Reddit 和未获批 Semantic Scholar 保持关闭。

## 模型可替换

本地两套独立字段（下列模型与地址只是示例，不是运行兜底）：

```env
SUB_AGENT_PROVIDER=dashscope
SUB_AGENT_MODEL=qwen3.7-plus
SUB_AGENT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
SUB_AGENT_LLM_API_KEY=
MAIN_AGENT_PROVIDER=dashscope
MAIN_AGENT_MODEL=qwen3.7-plus
MAIN_AGENT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MAIN_AGENT_LLM_API_KEY=
LLM_REQUEST_TIMEOUT_SECONDS=180
LLM_ENABLE_THINKING=false
```

上面的 Qwen 是示例，不是准入要求。MAIN 与 SUB 可以使用不同厂商、不同模型、不同地址和不同 Key；各自独立，不继承全局、不互相借用、不猜测厂商默认模型。提供方字段是 API 服务商标签（阿里云为 dashscope），不是必须写模型开发商；地址填 SDK 接口根地址，不含 /chat/completions 路径或密钥。接口须支持 Chat Completions 和完整 JSON。本地 Ollama 也须显式填写本角色 provider/model/base_url，只有 Key 可以免填。

Actions 对应：

| 类型 | 名称 | 说明 |
|---|---|---|
| Variable | SUB_AGENT_PROVIDER | 必填，初筛 API 服务商 |
| Variable | SUB_AGENT_MODEL | 必填，初筛/机制提取模型 ID |
| Variable | SUB_AGENT_BASE_URL | 必填，初筛接口根地址 |
| Secret | SUB_AGENT_LLM_API_KEY | SUB 专用 Key，云端必填 |
| Variable | MAIN_AGENT_PROVIDER | 必填，深挖 API 服务商 |
| Variable | MAIN_AGENT_MODEL | 必填，综合判断/人物/写作模型 ID |
| Variable | MAIN_AGENT_BASE_URL | 必填，深挖接口根地址 |
| Secret | MAIN_AGENT_LLM_API_KEY | MAIN 专用 Key，云端必填 |

设置路径：仓库 Settings → Secrets and variables → Actions。六个模型字段放 Variables → Repository variables；两个 Key 放 Secrets → Repository secrets。正式/Smoke 与单路线工作流均读取这两套配置；任一角色缺失时在调用模型前明确失败。source_check_only 不调用模型，因此不要求模型配置。

换某层模型时只改该层三项 Variables 和对应 Secret，另一层不受影响。不再读取旧 LLM_PROVIDER、LLM_MODEL、LLM_BASE_URL、LLM_API_KEY、DASHSCOPE_API_KEY、SUB_AGENT_API_KEY、MAIN_AGENT_API_KEY。部署前先配置两组新 Key 再删除旧项；GitHub 无法读回已有 Secret，须在 Secrets 页面重新填入，不把 Key 发到聊天。迁移可以先在两项 Secret 填同一个现有 Key，不必申请新 Key。信源 Key 不需要重置。

Doctor 只核对配置完整、URL 格式和范围，不再限制 qwen3.7。Smoke 才验证真实连接和响应；同 provider/model/url/Key 才共用角色检查。未知模型/代理不会收到猜测的 Qwen 参数。已核验官方 Qwen 配置使用显式非思考与总输出限制，历史证据见 [模型复核](RUN_38238493094_MODEL_REVIEW.md)。

独立 one-route-acceptance.yml 默认 3 天、单材料/单路线、两层合计 20 元内、核验当日 Qwen 价格保护；输入 arxiv_id 或 primary_url 二选一，并核对原始发布时间，不回溯旧样本。初筛使用 SUB，深挖/人物/写作使用 MAIN，两个实际接口须分别匹配已核验价格且共享一个总费用账本；任一不匹配就在首次请求前停止。价格契约不是通用生产模型限制。无邮件/生产状态，换模型后不要直接复用旧价格执行该测试，见 [单路线说明](ONE_ROUTE_ACCEPTANCE.md)。

## 研究量与取材

| Variable | 默认 | 语义 |
|---|---:|---|
| SOURCING_LOOKBACK_DAYS | 7 | 普通发现与本期发布窗口；Actions 手动用 lookback_days |
| PARADIGM_RECALL_OVERLAP_DAYS | 14 | 正式报告、重点研究者、官方入口回补 |
| PARADIGM_BOOTSTRAP_LOOKBACK_DAYS | 14 | 冷启动也保持有界，不引入 60 天积压 |
| PARADIGM_RESEARCH_ORIGIN_LIMIT | 48 | 本期跨发布者/来源轮转选定原点，1–500 |
| PARADIGM_RESEARCH_DEEP_LIMIT | 8 | 本轮深挖最多路线，1–50 |
| PARADIGM_RESEARCH_REFRESH_LIMIT | 4 | 最久未检查历史路线，0–50；0 关闭刷新 |
| PARADIGM_MIN_PRIMARY_SOURCES | 2 | 最少可用一手发现通道，1–9；不是独立复现数 |
| PARADIGM_SOURCE_QUERY_LIMIT | 100 | arXiv 每查询原点上限，1–1000，仍保留各车道 |
| PARADIGM_SOURCE_PAGE_LIMIT | 2 | OpenAlex 与已支持官方目录每查询分页上限，1–20 |
| PARADIGM_OFFICIAL_DETAIL_LIMIT | 6 | 每官方入口本进程新增详情上限，1–100 |

这些是研究排期和正常取材边界，不是技术淘汰，不声称全量完成。已有 GitHub Variables 会覆盖默认；显式 30/60 天不是废弃配置，会继续增加高信号回补范围。有效回补窗口不小于普通窗口，冷启动不小于回补窗口。若希望使用 14 天起点，再明确改值或删除相应覆盖，不在清理时静默改研究范围。

以下额外熔断默认仍为 0：PARADIGM_DISCOVERY_SAFETY_LIMIT、PARADIGM_ANALYSIS_SAFETY_LIMIT、PARADIGM_DEEP_SAFETY_LIMIT、PARADIGM_REPORT_SAFETY_LIMIT、PARADIGM_REFRESH_SAFETY_LIMIT。0 仅取消额外熔断，不取消 V0 有界研究计划。熔断/超时/未选不写成 Rubric 淘汰。

PARADIGM_RUBRIC_PATH、FRONTIER_LANDSCAPE_PATH 默认留空使用仓库量表/地图。PARADIGM_MIN_SUBSTANTIVE_DISCUSSIONS=2、PARADIGM_MIN_SECONDARY_ENGAGEMENT=50 是客观承接辅助边界，不是模型主观分。PARADIGM_ALLOW_UPDATES=true，相同证据签名不重复交付。

## 信源与增强

| 配置 | 默认/要求 |
|---|---|
| OPENALEX_API_KEY（Secret） | 推荐；论文与人物身份增强，未配置降级，不匿名补查 |
| OPENREVIEW_VENUES | 可选会议 group ID；按年份核对，不能填网页 URL |
| RESEARCH_FEED_URLS | 可选官方 RSS/Atom；不能填普通 HTML 首页 |
| RESEARCH_WATCHLIST_MODE | merge；自定义追加，replace 才替换内置目录 |
| PRIORITY_RESEARCH_PAGES | 留空用内置名单，添加不等于发布者背书 |
| PRIORITY_RESEARCH_LINK_SAFETY_LIMIT | 0；非零是官方详情额外熔断，仍受 V0 详情上限 |
| PRIORITY_RESEARCH_CONCURRENCY | 6，范围 1–12 |
| PRIORITY_BIBLIOGRAPHY_RESOLUTION_ENABLED | false；NYU 等无 ID 引用只有开启才搜索身份 |
| PRIORITY_BIBLIOGRAPHY_REQUEST_LIMIT | 8，范围 1–100，每进程，不是月度额度 |
| ESTABLISHED_RESEARCH_ORGANIZATIONS / MONITORED_RESEARCH_ORGANIZATIONS / PRIORITY_RESEARCHERS | 留空用内置；不要填整所大学/歧义裸词 |
| LESSWRONG_SOURCE_ENABLED / ALIGNMENT_FORUM_SOURCE_ENABLED / KOL_SOURCE_ENABLED | true，公开 RSS，无 Key |
| LESSWRONG_KARMA_THRESHOLD | 30，只是入选下限，不是精确 karma |
| KOL_CUSTOM_FEED_URLS | 可选，自定义无核验身份默认二次解读 |
| FOLLOW_BUILDERS_ENABLED / FOLLOW_BUILDERS_FEED_URL | true / 模板公开地址；不需要本地子模块 |
| GITHUB_TOKEN | Actions 自动注入 github.token，不另建同名 Secret |
| KOL_X_SOURCE_ENABLED / TWITTER_BEARER_TOKEN | false / 空；无免费 Token 不需要开启 |
| TWITTER_WATCH_ACCOUNTS | 留空用目录，不假称覆盖所有即时发言 |
| SEMANTIC_SCHOLAR_ENABLED / SEMANTIC_SCHOLAR_API_KEY | false / 空；获批后才开启，不匿名请求 |
| REDDIT_API_ACCESS_APPROVED | false；获得符合用途的批准后再开启 |
| REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USER_AGENT | 获批后的 OAuth 配置，Secret 不放 Variables |

正常 HTTP/API/RSS 抓取，不使用 Computer Use。反爬和单入口访问失败保留审计，不拖住健康路线。无标识书目不进入技术证据，未解析不等于没有论文，也不否决其他原点。

参考起步 Feed：Google Research、BAIR、OpenAI RSS 可按实际情况配置。合法空 Feed、登录 HTML、错误 XML 和解析不到文章必须区分；不要仅由 200 判定成功。

Tavily 可选配置：

```env
TAVILY_API_KEY=
TAVILY_SOCIAL_SEARCH_ENABLED=true
TAVILY_SOCIAL_SEARCH_DOMAINS=x.com,twitter.com,reddit.com,xiaohongshu.com
TAVILY_SOCIAL_MAX_RESULTS=12
TAVILY_DISCOVERY_DOMAINS=
TAVILY_REQUEST_SAFETY_LIMIT=0
TAVILY_MONTHLY_CREDIT_LIMIT=1000
```

正式组合检索由 TAVILY_DISCOVERY_DOMAINS 控制，留空全网索引；SOCIAL_SEARCH_DOMAINS 供 Smoke。结果不是平台全量或独立承接，也没有可用来推断声量的互动数字。

月度保护先核验实际 UTC 账户用量，再在 SQLite 原子预留，未知请求扣费不退款并停止该提供方后续搜索。0 明确关闭月度保护，不表示免费；账本不能锁住其他客户端，提供方 PAYG/硬限额仍需所有者控制。新 V0 epoch 的首个用量观察读取实际账户使用量，不把清库当作供应商额度清零。

独立 NYU/Tavily 验收的 included-only 费用保护更严格；它失败不等于生产 Key 无效。用户已暂不授权追加调用，不能自动复验或充值。见 [Key 指南](SOURCE_API_KEY_VERIFICATION.md)。

## 超时、并发与自动续跑

| Variable | 默认 |
|---|---:|
| LLM_REQUEST_TIMEOUT_SECONDS | 180 |
| SMOKE_CHECK_TIMEOUT_SECONDS | 30 |
| PARADIGM_RUN_BUDGET_SECONDS | 3600 |
| PARADIGM_STAGE_RESERVE_SECONDS | 1200 |
| PARADIGM_DISCOVERY_SOURCE_TIMEOUT_SECONDS | 600 |
| PARADIGM_REPORT_TIMEOUT_SECONDS | 1200 |
| PARADIGM_REPORT_REQUEST_TIMEOUT_SECONDS | 360 |
| PARADIGM_REPORT_ROUTE_CONCURRENCY | 2 |
| PARADIGM_REPORT_MAX_RENDER_ATTEMPTS | 3 |
| PARADIGM_ANALYSIS_BATCH_SIZE / PARADIGM_ORIGIN_PREFILTER_BATCH_SIZE | 6 / 24 |
| PARADIGM_ORIGIN_PREFILTER_ENABLED | true |
| PARADIGM_TECHNICAL_REPORT_MECHANISM_SLICE | 2 |
| PARADIGM_DEEP_BATCH_SIZE / PARADIGM_DEEP_CONCURRENCY | 1 / 2 |
| PARADIGM_RESEARCHER_PROFILE_LIMIT / PARADIGM_KEY_RESEARCHER_LIMIT | 6 / 3 |
| PARADIGM_PRIORITY_AUTHOR_SWEEP_ENABLED | true |
| PARADIGM_AUTO_RESUME_ENABLED | false |
| PARADIGM_AUTO_RESUME_MAX_RUNS / TOTAL_BUDGET_SECONDS / MAX_TOKENS | 4 / 14400 / 4000000 |

最后一行分别为 PARADIGM_AUTO_RESUME_MAX_RUNS、PARADIGM_AUTO_RESUME_TOTAL_BUDGET_SECONDS、PARADIGM_AUTO_RESUME_MAX_TOKENS。开启后也只处理健康状态上传后的同批纯预算延期；执行、覆盖故障和未知用量不自动扩大支出。tokens 是轮间停止线，不是人民币单轮硬费用上限。

同窗续跑保持选定计划。正常跨周运行归档旧未交付研究计划，待投递 outbox 仍优先恢复。研究诊断仍可含 pending；交付 v2 的范围凭据单独验证，不把诊断清零。

## 邮件与验收

```env
EMAIL_PUSH_ENABLED=true
EMAIL_PUSH_REQUIRED=true
EMAIL_MAX_ATTACHMENT_BYTES=10000000
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM=
SMTP_TO=
SMTP_USE_SSL=true
SMTP_USE_STARTTLS=false
```

应用密码/SMTP 授权码不是登录密码；SSL 与 STARTTLS 不得同时开启。Actions 默认 QQ SSL，SMTP_USERNAME/PASSWORD/TO 为 Secrets。发送失败不提前登记签名，已完成研究和合规报告保留。

本地时区参数 SCHEDULE_DAY_OF_WEEK=fri、SCHEDULE_HOUR=9、SCHEDULE_MINUTE=15、SCHEDULE_TIMEZONE=Asia/Shanghai。它们不改变 GitHub cron；云端要改调度须修改 Workflow 的 schedule。只选一种调度，避免本机/云端重复。

先零网络 Doctor 与离线门，再在获得授权后做 Smoke/正式运行。source_check_only 仅诊断输入，不测模型/SMTP。逐入口缺口不要求全部修完才能正式研究；--v0-readiness 只表示有足够有界输入能力样本，不证明生产 V0。正式验收检查实际研究、报告、邮件、状态和去重，不能只看 Actions 绿色。

云端 V0 使用 schema v10 与独立状态 epoch，第一次不恢复旧失败队列；后续恢复健康 V0 快照。见 [云端指南](CLOUD_AUTOMATION.md)。

## 冗余与废弃配置清理

| 名称/类型 | 状态与处理 |
|---|---|
| LLM_PROVIDER / LLM_MODEL / LLM_BASE_URL / LLM_API_KEY / DASHSCOPE_API_KEY / 旧角色 API_KEY | 已退出运行契约，使用两套角色配置后删除，不启用别名回退 |
| TAVILY_MAX_REQUESTS_PER_RUN | 已废弃且不被工作流消费；按本库单进程调用语义迁到 TAVILY_REQUEST_SAFETY_LIMIT，再删除旧名称 |
| PRIORITY_RESEARCH_MAX_LINKS_PER_PAGE | 已废弃，不是 PARADIGM_OFFICIAL_DETAIL_LIMIT 的同义项；删除它不改变当前默认每入口 6 条详情，不自动套用旧值 |
| 手动建的 GITHUB_TOKEN Secret | 工作流自动注入 github.token；本地独立 Token 可以需要，但云端同名 Repository Secret 不需要 |
| 额外 SAFETY_LIMIT、并发/批次、冷启动/回补、不同搜索域名 | 仍有实际消费者，语义不同，不当作重复配置删除；通常使用默认即可 |
| legacy / 本地专用参数 | 默认云端不消费，不需要为 V0 建 Variables；保留后端兼容能力，不删除历史证据或用户本地配置 |

2026-10-14 配置审计：两项旧源参数清理时，Tavily 的旧 12 次限制迁到现行名称；旧目录值 4 不映射到不同语义的新详情参数；仍生效的 30 天回补保留。模型和 SMTP Secrets 不轮换、不读回。静态/离线清理不证明提供方实际连通性或研究质量。
