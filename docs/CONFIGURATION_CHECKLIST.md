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

## B. 强烈建议配置

- [ ] Semantic Scholar API Key
- [ ] GitHub Token
- [ ] 4 个以上官方研究 RSS/Atom Feed
- [ ] SMTP 邮件推送

Semantic Scholar：

1. 打开 https://www.semanticscholar.org/product/api 。
2. 选择 “Request an API Key”，Key 会通过邮件发送。
3. 填写：

```env
SEMANTIC_SCHOLAR_API_KEY=你的Semantic-Scholar-Key
```

项目已经按照官方默认额度限制为每秒最多一个请求。Key 未获批前可以留空运行，但作者履历和引用增强更容易被限流。

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
PARADIGM_ALLOW_UPDATES=true
```

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

## G. 当前已知配置状态

截至最近一次静态体检：

- [x] 主模型：`dashscope/qwen3.7-plus`
- [x] 子模型：`dashscope/qwen3.7-plus`
- [x] GitHub Token 已配置
- [x] Codex 每周五自动任务已创建
- [ ] OpenAlex Key 尚未配置
- [ ] Semantic Scholar Key 尚未配置
- [ ] 官方研究 Feed 尚未配置
- [ ] SMTP 尚未启用/配置完整
