# AI 技术范式雷达：GitHub 云端自动化上线清单

## 运行结果

- 每周五 09:15（Asia/Shanghai）由 GitHub 云端运行，本机无需开机。
- GitHub Actions 页面提供“Run workflow”按钮，可以随时手动执行，并选择 7 天或 30 天窗口。
- 自动与手动触发都执行 `python main.py`，报告生成后都会发送邮件。
- SMTP 发送失败会让任务失败，不会把该报告登记成已成功交付。
- 报告若包含英文长段、评分表、字段拼装或缺少核心章节，会先自动重写一次；仍不合格则任务失败且不发送邮件。
- 去重数据库会在成功运行后保存为私有 Actions artifact；下一次运行先恢复，防止跨周重复。

## 1. 私有 GitHub 仓库

项目应保存在 **Private** GitHub 仓库。推送前务必确认 `.env` 和 `*.db` 没有进入提交；它们已在 `.gitignore` 中排除。

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
| `TAVILY_API_KEY` | 推荐 | Tavily 普通账号的 API Key；免费层用于发现公开索引的 X/Reddit/小红书页面 |
| `REDDIT_CLIENT_ID` | 否 | Reddit 批准 Data API 访问后创建的 OAuth Client ID |
| `REDDIT_CLIENT_SECRET` | 否 | 对应 OAuth Client Secret；不得放在 Variable |

不要创建名为 `GITHUB_TOKEN` 的 Secret。GitHub 会为每次运行自动提供权限受限的临时 token，工作流已直接使用。

## 3. 配置 Actions Variables

同一页面切换到 `Variables`，按需添加：

| Variable | 填写内容 |
|---|---|
| `OPENREVIEW_VENUES` | 逗号分隔的 venue id |
| `RESEARCH_FEED_URLS` | 已验证的官方 RSS/Atom 地址，逗号分隔 |
| `PRIORITY_RESEARCH_PAGES` | 已核验的官方研究首页；缺省时使用项目内置的 Moonshot/OpenAI/Anthropic/DeepMind/Meta AI/Qwen 列表 |
| `ESTABLISHED_RESEARCH_ORGANIZATIONS` | 能提供研究与传播势能的前沿组织，逗号分隔；普通大学不要仅凭名称加入 |
| `REDDIT_API_ACCESS_APPROVED` | 只有收到 Reddit 批准且用途符合许可时填 `true`；否则保持空或 `false` |
| `REDDIT_USER_AGENT` | 例如 `python:ai-paradigm-radar:v1.0 (by /u/你的用户名)`；不含密钥，可放 Variable |

不配置变量时，RSS 信源为空；高优先级官方页面和前沿组织使用项目默认值，不影响工作流语法。

Tavily 本地开关、域名和结果数已有保守默认值，GitHub 上通常只需添加 `TAVILY_API_KEY`。Reddit 的 Client ID/Secret 必须和“已批准”开关一起配置；只填密钥但不开启批准开关时，代码不会请求 Reddit API。

## 4. 首次手动验收

1. 打开仓库的 `Actions`。
2. 选择 `AI 技术范式雷达`。
3. 点击 `Run workflow`，第一次选择 `7` 天。
4. 确认“配置体检”“抓取、分析并发送邮件”“保存跨周去重状态”全部为绿色。
5. 确认收件箱收到邮件，并在该次运行的 Artifacts 中看到报告与 `paradigm-radar-state`。

首次成功后不需要再保持电脑开机。以后每周五由 GitHub 执行；网页手动运行和定时运行共享同一份去重状态。

## 5. 运维注意事项

- 不要同时长期运行本机 `python main.py --schedule` 或重复的 Codex 自动任务，否则可能在同一天收到两封邮件。
- GitHub 定时任务可能因平台负载稍有延迟，所以安排在 09:15 而不是整点。
- 状态和报告 artifact 当前保留 90 天；只要任务每周持续成功，下一周就能恢复最近状态。
- 如果连续超过 90 天没有成功运行，artifact 可能过期，下一次会被视为新的首跑。
- 周报没有合格路线时仍会成功发送“空雷达”；这是研究结论，不是任务故障。只有编辑质量闸门或邮件投递失败才会阻止状态保存。
- GitHub 公共仓库连续 60 天无活动可能停用 scheduled workflow，因此本项目建议使用私有仓库。
- 修改工作流后，确保更改已经进入默认分支。
- 工作流使用 Node 24 版本的 `checkout@v6`、`setup-python@v6` 与 `upload-artifact@v7`；任务不执行 git push，因此 checkout 不持久化临时凭据。
