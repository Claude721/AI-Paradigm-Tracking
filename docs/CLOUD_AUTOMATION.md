# AI 技术范式雷达：GitHub 云端自动化上线清单

## 运行结果

- 每周五 09:15（Asia/Shanghai）由 GitHub 云端运行，本机无需开机。
- GitHub Actions 页面提供“Run workflow”按钮，可以随时手动执行，并选择 7 天或 30 天窗口。
- 手动运行还可以勾选 `reset_state`，强制忽略旧数据库。
- `smoke_only=true` 时只做小成本真实接口验证：Qwen 只回复一次 `OK`，Tavily 只消耗一个 basic request，SMTP 只登录不发信；不会生成报告或改动去重数据库。
- 自动与手动触发都执行 `python main.py`，报告生成后都会发送邮件。
- SMTP 发送失败会让任务失败，不会把该报告登记成已成功交付。
- 报告若包含英文长段、评分表、字段拼装或缺少核心章节，会先自动重写一次；仍不合格则任务失败且不发送邮件。
- 成功邮件除研究 Memo 外，还会附带本轮结构化筛选审计和运行日志；审计记录信源返回量、筛选理由及各阶段 token 用量，不保存 prompt、模型正文或私有推理。
- 去重数据库会在成功运行后保存为私有 Actions artifact；只有代码 commit SHA 相同才会恢复。代码更新后的第一次运行自动从空数据库开始，防止 V0 旧数据污染新逻辑。

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
| `SEMANTIC_SCHOLAR_ENABLED` | 没有获批 Key 时填 `false`；只有同时创建 Key Secret 时才填 `true` |
| `RESEARCH_FEED_URLS` | 已验证的官方 RSS/Atom 地址，逗号分隔 |
| `RESEARCH_WATCHLIST_MODE` | 推荐 `merge`；自定义值追加到仓库内置目录，只有明确需要时才用 `replace` |
| `PRIORITY_RESEARCH_PAGES` | 可选的额外官方研究索引；留空使用内置海内外目录，追加页面不会自动成为权威背书 |
| `PRIORITY_RESEARCH_MAX_LINKS_PER_PAGE` | 每个入口每轮最多读取的近期链接数，默认 `4` |
| `PRIORITY_RESEARCH_CONCURRENCY` | 官方研究页并发数，默认 `6`，不建议超过 `12` |
| `ESTABLISHED_RESEARCH_ORGANIZATIONS` | 可选的额外已核验组织/具名实验室别名；禁止填整所大学和歧义短词 |
| `MONITORED_RESEARCH_ORGANIZATIONS` | 可选的额外监测组织；只增加关注，不因品牌自动放行 |
| `PRIORITY_RESEARCHERS` | 可选的额外重点研究者姓名；必须与公开主页或学术 ID 联合核验 |
| `REDDIT_API_ACCESS_APPROVED` | 只有收到 Reddit 批准且用途符合许可时填 `true`；否则保持空或 `false` |
| `REDDIT_USER_AGENT` | 例如 `python:ai-paradigm-radar:v1.0 (by /u/你的用户名)`；不含密钥，可放 Variable |
| `TAVILY_MAX_REQUESTS_PER_RUN` | 整轮 Tavily hard cap，推荐 `12`；候选再多也不会超过它 |
| `PARADIGM_MAX_ANALYSIS_ITEMS` | 本轮最多做机制抽取的原始材料数，推荐 `30` |
| `PARADIGM_MAX_DEEP_CANDIDATES` | 本轮最多做外部证据与主模型深挖的路线数，推荐 `16` |

不配置变量时，RSS 信源为空；研究入口、组织与重点研究者使用仓库中的版本化默认目录，不影响工作流语法。若仓库已有旧版 `PRIORITY_RESEARCH_PAGES` 或 `ESTABLISHED_RESEARCH_ORGANIZATIONS` 长名单，`merge` 会保留它们并同时加载新默认目录，不会冻结后续更新。

Tavily 本地开关、域名、结果数与整轮预算已有保守默认值，GitHub 上通常只需添加 `TAVILY_API_KEY`。Reddit 的 Client ID/Secret 必须和“已批准”开关一起配置；只填密钥但不开启批准开关时，代码不会请求 Reddit API。Semantic Scholar 同理：Secret 留空、Variable 为 `false` 时，代码不会匿名请求。

## 4. 首次手动验收

1. 打开仓库的 `Actions`。
2. 选择 `AI 技术范式雷达`。
3. 点击 `Run workflow`，第一次选择 `7` 天，保持 `smoke_only=true`。此时 `reset_state` 不影响结果。
4. 确认“配置体检”和“小成本真实接口冒烟”均为绿色，并下载 `paradigm-radar-audit-*` 查看 `smoke_test_latest.json`。
5. 再次点击 `Run workflow`，把 `smoke_only` 改成 `false`，把 `reset_state` 改成 `true`，执行新版本第一次完整运行。
6. 确认“抓取、分析并发送邮件”“保存跨周去重状态”全部为绿色。
7. 确认收件箱收到邮件及运行审计附件，并在该次运行的 Artifacts 中看到报告、`paradigm-radar-state` 与 `paradigm-radar-audit-*`。

首次成功后不需要再保持电脑开机。以后每周五由 GitHub 执行；网页手动运行和定时运行共享同一份去重状态。

## 5. 运维注意事项

- 不要同时长期运行本机 `python main.py --schedule` 或重复的 Codex 自动任务，否则可能在同一天收到两封邮件。
- GitHub 定时任务可能因平台负载稍有延迟，所以安排在 09:15 而不是整点。
- 状态和报告 artifact 当前保留 90 天；只要任务每周持续成功，下一周就能恢复最近状态。
- 运行审计 artifact 使用 `if: always()`：即使报告生成或邮件失败，也会尽量保留 `current_run.log` 和结构化审计，避免只看到一个红叉而不知道停在哪一步。
- 如果连续超过 90 天没有成功运行，artifact 可能过期，下一次会被视为新的首跑。
- 每次代码 commit 变化后，工作流会自动放弃旧状态；同一 commit 的手动重跑和后续周任务才会恢复去重数据库。V0 阶段若想在同一 commit 下再次清空，可手动勾选 `reset_state`。
- 周报没有合格路线时仍会成功发送“空雷达”；这是研究结论，不是任务故障。只有编辑质量闸门或邮件投递失败才会阻止状态保存。
- GitHub 公共仓库连续 60 天无活动可能停用 scheduled workflow，因此本项目建议使用私有仓库。
- 修改工作流后，确保更改已经进入默认分支。
- 工作流使用 Node 24 版本的 `checkout@v6`、`setup-python@v6` 与 `upload-artifact@v7`；任务不执行 git push，因此 checkout 不持久化临时凭据。
