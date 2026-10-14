# V0 配置清单

生产密钥仅放本地 .env 或 GitHub Actions Secrets，不写进聊天、Variables、日志或 Git。模板为 [.env.example](../.env.example)。

## 最小配置

- PIPELINE_MODE=paradigm。
- 一套能完成 Chat Completions / JSON 的模型 API，模型名、地址和 Key 对应。
- 默认公开来源保持开启；运行时至少 2 个一手发现通道可用。OpenAlex 与 GitHub 推荐，但不是全部信源都必须配置/成功。
- 如需邮件，SMTP 配置完整；已有发信功能不需要重新设计。
- 自动续跑、X、未获批 Reddit 和未获批 Semantic Scholar 保持关闭。

## 模型可替换

本地全局字段：

```env
LLM_PROVIDER=dashscope
LLM_MODEL=qwen3.7-plus
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=
LLM_REQUEST_TIMEOUT_SECONDS=180
LLM_ENABLE_THINKING=false
SUB_AGENT_PROVIDER=
SUB_AGENT_MODEL=
SUB_AGENT_BASE_URL=
SUB_AGENT_API_KEY=
MAIN_AGENT_PROVIDER=
MAIN_AGENT_MODEL=
MAIN_AGENT_BASE_URL=
MAIN_AGENT_API_KEY=
```

上面的 Qwen 是示例，不是准入要求。可以填写其他提供方/代理的实际模型 ID、兼容地址和 Key；不支持 Chat Completions 或不能稳定输出完整 JSON 的接口需要更换/适配，不靠品牌名字判断能力。LLM_PROVIDER 是 API 服务商标签（阿里云为 dashscope），不是必须写模型开发商；其他兼容服务商也可以使用其实际标签。LLM_BASE_URL 填 SDK 接口根地址，不含 /chat/completions 路径或密钥。角色字段逐项继承全局，切换角色提供方时一起覆盖模型、地址、Key，防止旧地址与新 Key 混用。

Actions 对应：

| 类型 | 名称 | 说明 |
|---|---|---|
| Variable | LLM_PROVIDER | 必填，当前为 dashscope（阿里云百炼） |
| Variable | LLM_MODEL | 必填，当前为 qwen3.7-plus |
| Variable | LLM_BASE_URL | 必填，当前为 https://dashscope.aliyuncs.com/compatible-mode/v1 |
| Secret | LLM_API_KEY | 通用模型 Key；留空回退已有 DASHSCOPE_API_KEY |
| Variables | SUB_AGENT_PROVIDER / MODEL / BASE_URL | 可选，留空继承全局 |
| Secret | SUB_AGENT_API_KEY | 可选，留空继承全局 |
| Variables | MAIN_AGENT_PROVIDER / MODEL / BASE_URL | 可选，留空继承全局 |
| Secret | MAIN_AGENT_API_KEY | 可选，留空继承全局 |

设置路径：仓库 Settings → Secrets and variables → Actions → Variables → Repository variables。正式/Smoke 与单路线工作流均直接读取这三个全局 Variables；缺失时在调用模型前明确失败，不静默回退旧模型。source_check_only 不调用模型，因此不要求这三个字段。

表中的角色缩写分别表示 SUB_AGENT_MODEL、SUB_AGENT_BASE_URL、MAIN_AGENT_MODEL、MAIN_AGENT_BASE_URL。换模型时改三个全局 Variables，并在 Secrets 更新 LLM_API_KEY；现有阿里云 Key 无需搬迁，仍兼容 DASHSCOPE_API_KEY。若同时存在两者，LLM_API_KEY 优先。清空不需要的角色 Variables **及角色 API Key Secrets**，否则角色专属值仍优先；没有角色覆盖时，初筛、深挖和写作一起继承全局。不必修改代码，也不要为换模型重置所有信源 Key。

Doctor 只核对配置完整、URL 格式和范围，不再限制 qwen3.7。Smoke 才验证真实连接和响应；同 provider/model/url/Key 才共用角色检查。未知模型/代理不会收到猜测的 Qwen 参数。已核验官方 Qwen 配置使用显式非思考与总输出限制，历史证据见 [模型复核](RUN_38238493094_MODEL_REVIEW.md)。

独立 one-route-acceptance.yml 默认 3 天、单材料/单路线、20 元内、核验当日 Qwen 价格保护；输入 arxiv_id 或 primary_url 二选一，并核对原始发布时间，不回溯旧样本。测试模型继承全局配置，但价格契约不匹配时停止；它不是通用生产模型限制。无邮件/生产状态，换模型后不要直接复用旧价格执行该测试，见 [单路线说明](ONE_ROUTE_ACCEPTANCE.md)。

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

这些是研究排期和正常取材边界，不是技术淘汰，不声称全量完成。已有 GitHub Variables 会覆盖新默认，部署前把旧 30/60 天回补改为 14 或删除它们以使用默认。

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

本地时区参数 SCHEDULE_DAY_OF_WEEK=fri、SCHEDULE_HOUR=9、SCHEDULE_MINUTE=15、SCHEDULE_TIMEZONE=Asia/Shanghai。只选一种调度，避免本机/云端重复。

先零网络 Doctor 与离线门，再在获得授权后做 Smoke/正式运行。source_check_only 仅诊断输入，不测模型/SMTP。逐入口缺口不要求全部修完才能正式研究；--v0-readiness 只表示有足够有界输入能力样本，不证明生产 V0。正式验收检查实际研究、报告、邮件、状态和去重，不能只看 Actions 绿色。

云端 V0 使用 schema v10 与独立状态 epoch，第一次不恢复旧失败队列；后续恢复健康 V0 快照。见 [云端指南](CLOUD_AUTOMATION.md)。
