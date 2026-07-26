# AI 技术范式雷达：完整配置 Checklist

> 所有密钥只填写到根目录 `.env`，不要粘贴到聊天、截图或提交到 Git。配置模板见 `.env.example`。

## A. 必须配置

- [ ] `PIPELINE_MODE=paradigm`
- [ ] DashScope 模型配置完整
- [ ] 主、子模型均为 `qwen3.7-plus`
- [ ] `OPENALEX_API_KEY` 已配置
- [ ] 至少配置两个有效 OpenReview venue
- [ ] 回看窗口和周任务时区确认

推荐模型配置：

```env
PIPELINE_MODE=paradigm

LLM_PROVIDER=dashscope
LLM_MODEL=qwen3.7-plus
LLM_API_KEY=你的百炼API-Key
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

SUB_AGENT_PROVIDER=dashscope
SUB_AGENT_MODEL=qwen3.7-plus
MAIN_AGENT_PROVIDER=dashscope
MAIN_AGENT_MODEL=qwen3.7-plus
```

`SUB_AGENT_API_KEY` 和 `MAIN_AGENT_API_KEY` 可以留空继承 `LLM_API_KEY`；如果分别填写，也应使用有效的 DashScope Key。

OpenAlex：

1. 打开 https://openalex.org/settings/api 并注册/登录。
2. 复制免费 API Key。
3. 填写：

```env
OPENALEX_API_KEY=你的OpenAlex-Key
```

OpenReview 推荐起步配置：

```env
OPENREVIEW_VENUES=ICLR.cc/2026/Conference,NeurIPS.cc/2026/Conference,NeurIPS.cc/2026/Evaluations_and_Datasets_Track,ICML.cc/2026/Conference
```

Venue ID 来自 OpenReview 页面 URL 中 `group?id=` 后面的部分，不能带结尾 `/`。每年会议切换时更新年份。

## B. 推荐增强项

- [ ] Semantic Scholar API Key（可选；没有学术邮箱就留空）
- [ ] GitHub Token
- [ ] 4 个以上官方研究 RSS/Atom Feed
- [ ] 高优先级官方研究页面保留默认值或已核验覆盖
- [ ] Follow Builders 二次传播 Feed 已启用
- [ ] 如需按工作标题搜索作者/KOL，配置 X API Bearer Token（可选）
- [ ] SMTP 邮件推送

Semantic Scholar（可选）：

1. 打开 https://www.semanticscholar.org/product/api 。
2. 选择 “Request an API Key”，Key 会通过邮件发送。
3. 填写：

```env
SEMANTIC_SCHOLAR_ENABLED=true
SEMANTIC_SCHOLAR_API_KEY=你的Semantic-Scholar-Key
```

项目已经按照官方默认额度限制为每秒最多一个请求。Key 未获批时使用：

```env
SEMANTIC_SCHOLAR_ENABLED=false
SEMANTIC_SCHOLAR_API_KEY=
```

此时程序会完整跳过 Semantic Scholar，绝不会退回匿名请求；人物身份改由 OpenAlex、ORCID 与已核验个人主页补齐。

GitHub：

1. 打开 https://github.com/settings/tokens 。
2. 创建只读 Token；本项目只搜索公开仓库，不需要写权限。
3. 填写：

```env
GITHUB_TOKEN=你的GitHub-Token
```

官方研究 Feed 推荐起步值：

```env
RESEARCH_FEED_URLS=https://research.google/blog/rss/,https://bair.berkeley.edu/blog/feed.xml,https://openai.com/news/rss.xml
```

只填写 RSS/Atom XML 地址，不能填写普通博客首页。单个 Feed 失效时系统会跳过，不影响其他 Feed。

高优先级官方页面用于补足“重要机构没有 RSS、Technical Report 尚未进入 arXiv”的缺口。完整默认目录版本化保存在 `research_watchlist.py`，覆盖海内外基础模型公司、World Model/机器人组织和具名高校实验室。通常保持以下值即可：

```env
RESEARCH_WATCHLIST_MODE=merge
PRIORITY_RESEARCH_PAGES=
PRIORITY_RESEARCH_LINK_SAFETY_LIMIT=0
PRIORITY_RESEARCH_CONCURRENCY=6
ESTABLISHED_RESEARCH_ORGANIZATIONS=
MONITORED_RESEARCH_ORGANIZATIONS=
PRIORITY_RESEARCHERS=
```

`merge` 表示环境变量只追加少量自定义项，仓库更新时仍会获得新的内置名单；`replace` 才会完全替换默认目录。GitHub 中曾保存过旧版长名单也不需要删除，默认会去重合并。新增页面只提高召回优先级：只有内置目录记录了 owner 与 tier 的页面才继承发布者身份，用户追加页面只能同域抓取并降为 `verified`。

入口与名单分为四层：Priority pages 负责主动召回；`ESTABLISHED_RESEARCH_ORGANIZATIONS` 用于身份可核验、持续产出前沿研究的公司研究组织与具名实验室；`MONITORED_RESEARCH_ORGANIZATIONS` 保证模型厂商、机器人公司等被观察，但不会仅凭品牌自动放行；`PRIORITY_RESEARCHERS` 只在姓名精确匹配且已有主页、ORCID/OpenAlex 等公开身份后，提供人物轨迹线索。不要加入整所大学或 `AI Lab`、`Seed`、`GLM`、`ARC Lab` 等歧义裸词。完整名单与选择依据见 [前沿机构与研究者 Watchlist](FRONTIER_RESEARCH_WATCHLIST.md)。

KOL、播客与技术博客二次传播候选：

```env
FOLLOW_BUILDERS_ENABLED=true
FOLLOW_BUILDERS_FEED_URL=https://raw.githubusercontent.com/zarazhangrui/follow-builders/main
```

可选的 X 精确标题搜索：

```env
TWITTER_BEARER_TOKEN=你的X-API-Bearer-Token
```

不配置时自动跳过，不影响论文、GitHub 与 Hacker News。配置后系统会按工作标题搜索最近帖子，并读取公开账号简介与粉丝规模。作者本人发帖只用于身份核验；非作者的高关注账号解读、互动和讨论内容才进入外部势能判断。小红书目前没有稳定的官方公开搜索 API，因此本项目不做不可靠的页面抓取。

推荐先配置 Tavily 免费层，作为 X、Reddit、小红书和独立技术博客的公开网页发现兜底：

```env
TAVILY_API_KEY=tvly-你的Key
TAVILY_SOCIAL_SEARCH_ENABLED=true
TAVILY_SOCIAL_SEARCH_DOMAINS=x.com,twitter.com,reddit.com,xiaohongshu.com
TAVILY_SOCIAL_MAX_RESULTS=12
TAVILY_DISCOVERY_DOMAINS=
TAVILY_REQUEST_SAFETY_LIMIT=0
```

在 Tavily 创建普通账号即可，不要求学术邮箱，也不要求信用卡。免费计划额度以 Tavily 控制台当前显示为准；项目对每个通过 Rubric 的深挖候选采用一次 `basic` 组合搜索，不会分别为三个平台重复消耗。`TAVILY_DISCOVERY_DOMAINS` 留空时执行全网发现，能找到独立技术博客；需要限制站点时再填域名。`TAVILY_REQUEST_SAFETY_LIMIT=0` 表示动态覆盖全部深挖候选；只有你需要严格保护 credits 时才设置非零熔断值。搜索结果只能说明页面被公开索引，不能证明完整覆盖或用相关度替代点赞、评论等声量指标。

Reddit 若要获得帖子分数、评论量和讨论正文，需要先向 Reddit 申请 Data API 访问。获批后配置：

```env
REDDIT_API_ACCESS_APPROVED=true
REDDIT_CLIENT_ID=你的OAuth客户端ID
REDDIT_CLIENT_SECRET=你的OAuth客户端Secret
REDDIT_USER_AGENT=python:ai-paradigm-radar:v1.0 (by /u/你的Reddit用户名)
```

- [ ] 已从 Reddit 收到 API 访问批准；尚未获批时保持 `false`
- [ ] 若项目用于商业、投资或企业研究，申请中已明确用途并取得相应许可
- [ ] User-Agent 唯一、可识别，包含应用名、版本和 Reddit 用户名
- [ ] Client Secret 只放 `.env` 或 GitHub Repository secret

项目使用 OAuth 应用身份做低频标题搜索。Reddit 帖子与少量评论正文只在当轮综合时使用，随后清除；数据库和邮件保留原帖链接、互动指标和中文提炼结果。

## C. 邮件推送（选择一个邮箱）

- [ ] 邮箱已开启 SMTP
- [ ] 使用应用密码/SMTP 授权码，而不是登录密码
- [ ] 发件地址和收件地址已填写
- [ ] SSL 与 STARTTLS 没有同时开启

通用 SSL 配置：

```env
EMAIL_PUSH_ENABLED=true
EMAIL_PUSH_REQUIRED=true
SMTP_HOST=邮箱服务商的SMTP服务器
SMTP_PORT=465
SMTP_USERNAME=完整发件邮箱
SMTP_PASSWORD=SMTP授权码或应用密码
SMTP_FROM=完整发件邮箱
SMTP_TO=你的收件邮箱
SMTP_USE_SSL=true
SMTP_USE_STARTTLS=false
```

常见服务器：

| 邮箱 | `SMTP_HOST` | 端口 | 模式 |
|---|---|---:|---|
| QQ 邮箱 | `smtp.qq.com` | 465 | SSL |
| 163 邮箱 | `smtp.163.com` | 465 | SSL |
| Gmail | `smtp.gmail.com` | 465 | SSL |
| Outlook/Microsoft 365 | `smtp.office365.com` | 587 | STARTTLS |

使用 Outlook 587 时改为：

```env
SMTP_PORT=587
SMTP_USE_SSL=false
SMTP_USE_STARTTLS=true
```

Gmail 需要先启用两步验证，再生成应用密码。

## D. Rubric 与运行熔断

建议第一次整体运行保持默认值：

```env
PARADIGM_RUBRIC_PATH=
FRONTIER_LANDSCAPE_PATH=
PARADIGM_DISCOVERY_SAFETY_LIMIT=0
PARADIGM_ANALYSIS_SAFETY_LIMIT=0
PARADIGM_DEEP_SAFETY_LIMIT=0
PARADIGM_REPORT_SAFETY_LIMIT=0
PARADIGM_REFRESH_SAFETY_LIMIT=0
PARADIGM_MIN_SUBSTANTIVE_DISCUSSIONS=2
PARADIGM_MIN_SECONDARY_ENGAGEMENT=50
PARADIGM_ALLOW_UPDATES=true
PARADIGM_RECALL_OVERLAP_DAYS=14
PARADIGM_BOOTSTRAP_LOOKBACK_DAYS=60
PARADIGM_SEED_ARXIV_IDS=
PARADIGM_RESEARCHER_PROFILE_LIMIT=6
```

技术去留由仓库中的 `rubrics/paradigm_rubric.json` 决定；行业覆盖由 `taxonomy/frontier_landscape.json` 决定。前者维护判断问题，后者维护基础模型、推理与软件 Agent、多模态/语音/3D、具身/自动驾驶、World Model、AI4S、系统/端侧/硬件、安全等发现范围。通常两个路径都留空，直接维护仓库内版本并提交。覆盖地图的 `version` 改变后，云端会重新扫描 60 天，避免新增领域继续继承旧召回盲区。

五个 `*_SAFETY_LIMIT` 均为 `0` 时不限制数量：本轮会评估全部召回材料，并深挖全部通过初筛 Rubric 的路线。非零值只是用户主动开启的成本/运行熔断，不是 Top-K；被熔断的内容在审计中标记为“未完成”，不得写成“未通过”。旧版 `PARADIGM_MIN_*` 与 `PARADIGM_MAX_*` 已停用，GitHub 中即使残留也不会影响新逻辑。

第一次运行前必须先做专用 smoke test。它每个接口只取极少结果、模型只要求回复 `OK`，SMTP 只登录不发信，不会创建报告或修改范式数据库。

## E. 时间窗口与自动任务

```env
SOURCING_LOOKBACK_DAYS=7
PARADIGM_RECALL_OVERLAP_DAYS=14
SCHEDULE_DAY_OF_WEEK=fri
SCHEDULE_HOUR=9
SCHEDULE_MINUTE=0
SCHEDULE_TIMEZONE=Asia/Shanghai
```

- [ ] 周报使用 7 天窗口；月度专题才改成 30 天
- [ ] `PARADIGM_RECALL_OVERLAP_DAYS=14`：扫描窗口重叠一周，由数据库去重并修复单周漏抓
- [ ] `PARADIGM_BOOTSTRAP_LOOKBACK_DAYS=60`：数据库为空或覆盖地图升级时自动建立近期基线
- [ ] 常规运行保持 `PARADIGM_SEED_ARXIV_IDS` 为空；只在精确回补/审计漏项时临时填写
- [ ] Codex 自动任务“AI 技术范式雷达周报”保持启用
- [ ] 不要同时长期运行 `python main.py --schedule`，避免形成两套调度

如果只在本机使用，Codex 自动任务或 `python main.py --schedule` 都要求电脑保持开机。
需要关机后仍执行时，使用仓库内置的 GitHub Actions 云端任务，并停用本机重复调度。
具体见 [`CLOUD_AUTOMATION.md`](CLOUD_AUTOMATION.md)。

## F. 配置完成后的运行顺序

在项目根目录执行：

```bash
source venv/bin/activate
python main.py --doctor
python main.py --smoke-test
```

`--doctor` 只检查“有没有配置”；`--smoke-test` 才真实检查“凭据和接口能不能用”。结果同时写入 `logs/smoke_test_latest.json`，其中不含密钥或响应正文。只有配置过的接口失败时命令才返回非零；明确未配置的可选接口显示 `skipped`。

只有以下项目通过或明确跳过后再首跑：

- [ ] 两个 Qwen 模型均为 `qwen3.7-plus`
- [ ] OpenAlex 显示就绪
- [ ] Semantic Scholar 有 Key 时通过；没有 Key 时显示“不会匿名请求”并跳过
- [ ] OpenReview venue 数量正确
- [ ] 研究 Feed 数量大于 0
- [ ] 高优先级官方研究页面数量大于 0
- [ ] GitHub Token 就绪
- [ ] SMTP 显示配置完整

如果只想复核网络信源、不重复调用 Qwen 或 SMTP 登录：

```bash
python main.py --smoke-test --smoke-skip-llm --smoke-skip-smtp --smoke-skip-tavily
```

上面的复核命令不会重复消耗 Qwen、SMTP 或 Tavily；第一次完整 smoke 应直接运行 `python main.py --smoke-test`，让 Tavily 做 1 个 basic request。全部关键项通过后再执行 `python main.py`；如果 `EMAIL_PUSH_ENABLED=true`，手动完整运行也会发送邮件。

检查结果：

- [ ] `database/paradigm_radar.db` 已生成
- [ ] `reports/output/paradigm_radar_YYYY-MM-DD.md` 已生成
- [ ] 邮件标题为“AI 技术范式雷达”
- [ ] 邮件附件可以正常打开
- [ ] 日志中没有持续的 401、403 或 429
- [ ] 报告没有把普通 GitHub 项目当成独立范式
- [ ] 报告开头有约 500 字“本期研究 Memo”
- [ ] 最终报告不展示评分表或证据堆叠表
- [ ] 正文没有英文摘要、英文长句或上游字段原样摘录
- [ ] 多篇同 background 工作被组织成技术路线，而不是一篇一节机械展开
- [ ] 关键人物至少有机构/背景与身份检索记录；有公开主页或邮箱时已附链接
- [ ] 没有合格新范式时允许发送空雷达，不以周更频率硬凑内容

## G. GitHub Actions 配置位置

API Key、OAuth Client Secret、QQ 邮箱与 SMTP 授权码放在 **Repository secrets**；非敏感的 Reddit 批准开关、User-Agent、OpenReview venue、研究 Feed、高优先级官方页面与前沿组织名单放在 **Repository variables**。不要使用 Environment secrets，除非工作流同时显式绑定对应 environment。详见 [`CLOUD_AUTOMATION.md`](CLOUD_AUTOMATION.md)。
