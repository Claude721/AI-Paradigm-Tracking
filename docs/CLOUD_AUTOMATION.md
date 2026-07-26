# AI 技术范式雷达：GitHub 云端自动化上线清单

## 运行结果

- 每周五 09:15（Asia/Shanghai）由 GitHub 云端运行，本机无需开机。
- GitHub Actions 页面提供“Run workflow”按钮，可以随时手动执行，并选择 7/30/60/90 天窗口或填写精确 arXiv ID。
- 手动运行还可以勾选 `reset_state`，强制忽略旧数据库。
- `smoke_only=true` 时只做小成本真实接口验证：Qwen 只回复一次 `OK`，Tavily 只消耗一个 basic request，SMTP 只登录不发信；不会生成报告或改动去重数据库。
- 自动与手动触发都执行 `python main.py`，报告生成后都会发送邮件。
- SMTP 发送失败会让任务失败，不会把该报告登记成已成功交付。
- 报告若包含英文长段、评分表、字段拼装或缺少核心章节，会先自动重写一次；仍不合格则任务失败且不发送邮件。
- 成功邮件除研究 Memo 外，还会附带本轮结构化筛选审计和运行日志；审计记录信源返回量、筛选理由及各阶段 token 用量，不保存 prompt、模型正文或私有推理。
- 去重数据库会在成功运行后保存为私有 Actions artifact；状态 schema 兼容时跨代码 commit 恢复，避免每次改 prompt 都丢掉路线历史。只有主动勾选 `reset_state` 或 schema 不兼容时才从空状态开始；空状态自动使用 60 天冷启动窗口。

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
| `TAVILY_API_KEY` | 推荐 | Tavily 普通账号的 API Key；用于发现公开索引的社区页面和独立技术博客 |
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
| `PRIORITY_RESEARCH_LINK_SAFETY_LIMIT` | 官方入口 HTTP 抓取熔断；默认 `0`，读取日期窗口内全部候选链接 |
| `PRIORITY_RESEARCH_CONCURRENCY` | 官方研究页并发数，默认 `6`，不建议超过 `12` |
| `ESTABLISHED_RESEARCH_ORGANIZATIONS` | 可选的额外已核验组织/具名实验室别名；禁止填整所大学和歧义短词 |
| `MONITORED_RESEARCH_ORGANIZATIONS` | 可选的额外监测组织；只增加关注，不因品牌自动放行 |
| `PRIORITY_RESEARCHERS` | 可选的额外重点研究者姓名；必须与公开主页或学术 ID 联合核验 |
| `REDDIT_API_ACCESS_APPROVED` | 只有收到 Reddit 批准且用途符合许可时填 `true`；否则保持空或 `false` |
| `REDDIT_USER_AGENT` | 例如 `python:ai-paradigm-radar:v1.0 (by /u/你的用户名)`；不含密钥，可放 Variable |
| `TAVILY_REQUEST_SAFETY_LIMIT` | Tavily credit 熔断；默认 `0`，搜索全部通过 Rubric 的深挖候选 |
| `TAVILY_DISCOVERY_DOMAINS` | 默认留空执行全网发现；只有要限制 Tavily 站点时才填逗号分隔域名 |
| `PARADIGM_RECALL_OVERLAP_DAYS` | 推荐 `14`；周更重叠扫描并由数据库去重，修复单周接口失败或索引晚到 |
| `PARADIGM_BOOTSTRAP_LOOKBACK_DAYS` | 推荐 `60`；状态数据库为空或覆盖地图版本变化时使用 |
| `PARADIGM_RESEARCHER_PROFILE_LIMIT` | 推荐 `6`；覆盖前三位、末位/资深作者和重点研究者 |
| `PARADIGM_*_SAFETY_LIMIT` | 可选运行熔断；默认/推荐 `0`，表示数量完全由 Rubric 结果决定 |

不配置变量时，RSS 信源为空；研究入口、组织与重点研究者使用仓库中的版本化默认目录，不影响工作流语法。若仓库已有旧版 `PRIORITY_RESEARCH_PAGES` 或 `ESTABLISHED_RESEARCH_ORGANIZATIONS` 长名单，`merge` 会保留它们并同时加载新默认目录，不会冻结后续更新。

GitHub 上通常只需添加 `TAVILY_API_KEY`；`TAVILY_DISCOVERY_DOMAINS` 留空时同时发现社区和普通技术网页，结果仍只算索引线索。Rubric 与前沿覆盖地图都随代码提交，无需创建 Secret 或 Variable。旧版 `PARADIGM_MAX_ANALYSIS_ITEMS`、`PARADIGM_MAX_DEEP_CANDIDATES` 等 Variable 可以删除；即使保留，新代码也不会读取。Reddit 的 Client ID/Secret 必须和“已批准”开关一起配置；只填密钥但不开启批准开关时，代码不会请求 Reddit API。Semantic Scholar 同理：Secret 留空、Variable 为 `false` 时，代码不会匿名请求。

跨提交恢复状态会检查数据库 schema，并读取前沿覆盖地图版本，不再因为 commit SHA 改变就重置。普通 Prompt、Skill 或报告样式更新会延续跨周历史；覆盖地图升级时仍恢复旧数据库用于证据去重，但程序会用 60 天窗口补扫新加入的技术面。已经分析过且正文未变化的材料不会再次调用 LLM。

## 4. 首次手动验收

1. 打开仓库的 `Actions`。
2. 选择 `AI 技术范式雷达`。
3. 点击 `Run workflow`，第一次选择 `7` 天，保持 `smoke_only=true`。精确 arXiv ID 留空，此时 `reset_state` 不影响结果。
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
- 代码 commit 变化不会自动丢弃旧状态；数据库 schema 版本变化才会从空状态冷启动。覆盖地图版本变化会保留旧去重历史并扩大为 60 天补扫。V0 阶段确需清空所有历史时才手动勾选 `reset_state`。
- 周报没有合格路线时仍会成功发送“空雷达”；这是研究结论，不是任务故障。只有编辑质量闸门或邮件投递失败才会阻止状态保存。
- GitHub 公共仓库连续 60 天无活动可能停用 scheduled workflow，因此本项目建议使用私有仓库。
- 修改工作流后，确保更改已经进入默认分支。
- 工作流使用 Node 24 版本的 `checkout@v6`、`setup-python@v6` 与 `upload-artifact@v7`；任务不执行 git push，因此 checkout 不持久化临时凭据。
