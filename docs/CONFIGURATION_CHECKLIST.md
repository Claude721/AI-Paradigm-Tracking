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
SEMANTIC_SCHOLAR_API_KEY=你的Semantic-Scholar-Key
```

项目已经按照官方默认额度限制为每秒最多一个请求。Key 未获批时直接留空；引用增强可能更容易被限流，但人物身份仍会通过 OpenAlex、ORCID 与已核验个人主页补齐。

GitHub：

1. 打开 https://github.com/settings/tokens 。
2. 创建只读 Token；本项目只搜索公开仓库，不需要写权限。
3. 填写：

```env
GITHUB_TOKEN=你的GitHub-Token
```

官方研究 Feed 推荐起步值：

```env
RESEARCH_FEED_URLS=https://research.google/blog/rss/,https://deepmind.google/discover/blog/rss/,https://developer.nvidia.com/blog/feed,https://feeds.feedburner.com/nvidiablog,https://bair.berkeley.edu/blog/feed.xml
```

只填写 RSS/Atom XML 地址，不能填写普通博客首页。单个 Feed 失效时系统会跳过，不影响其他 Feed。

高优先级官方页面用于补足“重要机构没有 RSS、Technical Report 尚未进入 arXiv”的缺口。默认已经包含 Moonshot、OpenAI、Anthropic、DeepMind、Meta AI 与 Qwen：

```env
PRIORITY_RESEARCH_PAGES=https://www.moonshot.ai/,https://www.anthropic.com/research,https://openai.com/research/,https://deepmind.google/research/,https://ai.meta.com/research/,https://qwenlm.github.io/
```

这些地址必须是已经确认归属的官方研究入口，因为配置本身会成为“发布者已核验”的证据。普通大学或泛科技新闻站不要加入。前沿组织名单也可覆盖；空值会自动回到项目默认名单：

```env
ESTABLISHED_RESEARCH_ORGANIZATIONS=OpenAI,Anthropic,Google DeepMind,DeepMind,Meta AI,FAIR,Microsoft Research,NVIDIA,Moonshot AI,月之暗面,DeepSeek,Alibaba DAMO,Qwen,ByteDance Seed,xAI,Mistral AI,Cohere
```

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

推荐先配置 Tavily 免费层，作为 X、Reddit、小红书公开网页的发现兜底：

```env
TAVILY_API_KEY=tvly-你的Key
TAVILY_SOCIAL_SEARCH_ENABLED=true
TAVILY_SOCIAL_SEARCH_DOMAINS=x.com,twitter.com,reddit.com,xiaohongshu.com
TAVILY_SOCIAL_MAX_RESULTS=12
```

在 Tavily 创建普通账号即可，不要求学术邮箱，也不要求信用卡。免费计划每月 1,000 credits；项目对每个候选采用一次 `basic` 组合搜索（1 credit），不会分别为三个平台重复消耗。搜索结果只能说明页面被公开索引，不能证明完整覆盖或用相关度替代点赞、评论等声量指标。

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

## D. 范式筛选参数

建议第一次整体运行保持默认值：

```env
PARADIGM_MIN_SCORE=65
PARADIGM_MIN_NOVELTY=6
PARADIGM_MIN_SCOPE=6
PARADIGM_MAX_DISCOVERY_ITEMS=100
PARADIGM_MAX_REPORT_ITEMS=12
PARADIGM_MAX_REFRESH_ITEMS=40
PARADIGM_MIN_SUBSTANTIVE_DISCUSSIONS=2
PARADIGM_MIN_SECONDARY_ENGAGEMENT=50
PARADIGM_ALLOW_UPDATES=true
```

这里的分数只做内部排序。真正的准入顺序是：技术硬门槛 → 发布者/团队核验 → 独立讨论或承接。`PARADIGM_MAX_REFRESH_ITEMS` 控制每周重新检索新讨论的观察池规模；后两项只约束发布者背景尚不明确的候选，不会把作者本人的宣传帖计入独立讨论。

第一次运行可能产生较多 Qwen 调用。如需先做低成本 smoke test，可临时把 `PARADIGM_MAX_DISCOVERY_ITEMS` 改为 `10`；确认成功后恢复为 `100` 再做正式首跑。

## E. 时间窗口与自动任务

```env
SOURCING_LOOKBACK_DAYS=7
SCHEDULE_DAY_OF_WEEK=fri
SCHEDULE_HOUR=9
SCHEDULE_MINUTE=0
SCHEDULE_TIMEZONE=Asia/Shanghai
```

- [ ] 周报使用 7 天窗口；月度专题才改成 30 天
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
```

只有以下项目全部为就绪后再首跑：

- [ ] 两个 Qwen 模型均为 `qwen3.7-plus`
- [ ] OpenAlex 显示就绪
- [ ] Semantic Scholar 显示就绪或明确接受降级运行
- [ ] OpenReview venue 数量正确
- [ ] 研究 Feed 数量大于 0
- [ ] 高优先级官方研究页面数量大于 0
- [ ] GitHub Token 就绪
- [ ] SMTP 显示配置完整

建议先运行 10 篇 smoke test：

```env
PARADIGM_MAX_DISCOVERY_ITEMS=10
```

```bash
python main.py
```

确认数据库、报告和邮件都成功后，把上限恢复为 100，再运行正式首轮：

```env
PARADIGM_MAX_DISCOVERY_ITEMS=100
```

```bash
python main.py
```

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
