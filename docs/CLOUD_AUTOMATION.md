# AI 技术范式雷达：GitHub 云端自动化上线清单

## 运行结果

- 每周五 09:15（Asia/Shanghai）由 GitHub 云端运行，本机无需开机。
- GitHub Actions 页面提供“Run workflow”按钮，可以随时手动执行，并选择 7/30/60/90 天窗口或填写精确 arXiv ID。
- 手动运行还可以勾选 `reset_state`，强制忽略旧数据库。
- `smoke_only=true` 时只做小成本真实能力验证：Qwen 必须按契约回复 `OK`；arXiv、Hugging Face、OpenAlex、GitHub 等单端点能力只发一个最小请求，Follow Builders、OpenReview、官方研究页、LessWrong/KOL RSS 最多按配置顺序 failover 5 个入口并在首个成功后停止；Tavily 只消耗一个 basic request，SMTP 只登录不发信。每项默认 30 秒总时限，不会调用生产召回器、生成报告或改动去重数据库。
- 自动与手动触发都执行 `python main.py`，报告生成后都会发送邮件。
- 研究检查点、报告渲染和邮件投递已经解耦。研究完成后先写入持久化 outbox；报告或 SMTP 失败不会撤销已完成研究，也不会把该报告登记成已成功交付。缺少当前输入契约的冻结快照，或完成内置修订后仍违反确定性交付质量契约的制品会立即隔离；网络、超时等其他渲染失败默认累计 3 次后隔离，并把候选原子退回 `pending_deep`。隔离记录不再阻塞后续研究。SMTP 失败仍保留已验证报告并让必需邮件任务失败。成功续投旧 outbox 后当前进程安全退出，云端在新状态 artifact 上传成功后自动排队第二个完整研究 run；若旧任务被隔离，当前进程直接继续新研究。若本轮新生成的报告被隔离，状态 artifact 仍会保存，但当前 Workflow 必须失败，不能把未完成交付显示为绿色；下一次运行也不会再被该坏任务阻塞。
- 报告若包含英文长段、评分表、字段拼装、缺少关键人物/公开检索记录或缺少任一路线的一手链接，会先自动重写一次；仍不合格则任务失败且不发送邮件。人物与原文索引由结构化证据确定性生成，不依赖模型抄写；全文每一个 HTTP(S) URL 都必须原样来自证据或人物档案，一个有效原文不能掩盖另一个猜测链接。
- 成功邮件除研究 Memo 外，还会附带本轮结构化筛选审计和运行日志；审计记录信源返回量、筛选理由及各阶段 token 用量，不保存 prompt、模型正文或私有推理。
- 去重数据库会在生产运行结束后以 `always()` 语义保存为私有 Actions artifact，包括报告/SMTP 失败后留下的研究检查点与 outbox。当前 schema v6 除 SQLite `quick_check`、表/字段迁移、领域 JSON 与 fingerprint/key 核验外，还会检查活动 outbox 是否满足当前人物/一手来源输入契约；不可再交付的旧任务在迁移时隔离并把候选退回深挖。最新 artifact 即使数据库页完整、但领域 payload 已损坏，也会被拒绝并继续尝试更早的不可变快照。恢复成功后会执行 `python main.py --inspect-state`，在日志中输出不含候选正文和 Secret 的队列计数、截断 key、失败类型及尝试次数。除非手动勾选 `reset_state=true`，所有快照均缺失、损坏或不兼容都会 fail closed，不会静默冷启动。
- 工作流先在清空生产配置、拒绝网络和忽略 `.env` 的子进程中运行完整离线单元测试、无第三方包的失败提醒导入检查与静态编译，再接触生产状态和真实接口。任一普通生产步骤失败时，最后的 `always() && failure()` 步骤会尝试发送独立失败提醒，并指出首个失败步骤、运行 commit 与离线失败用例；不会再把回归测试失败误报成研究主流程失败。GitHub 直接取消整个 job、Runner 宕机或达到 90 分钟硬超时时，任何后置步骤都无法保证执行，因此必须依靠软预算主动收尾。若运行成功但召回、研究或交付仍未闭合，邮件主题会明确标注 `[研究未完成]`，审计会区分 Rubric 淘汰与运行延后；它不能被理解为“本期无新信号”。

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

不要创建名为 `GITHUB_TOKEN` 的 Secret。GitHub 会为每次运行自动提供权限受限的临时 token，工作流已直接使用。工作流声明 `actions: write` 只用于在历史续投完成并成功保存状态后触发下一次 `workflow_dispatch`；代码仍是 `contents: read`，不会推送或改写仓库。

## 3. 配置 Actions Variables

同一页面切换到 `Variables`，按需添加：

| Variable | 填写内容 |
|---|---|
| `LLM_REQUEST_TIMEOUT_SECONDS` | 推荐 `180`；单次模型请求硬上限，业务层另有一次可审计重试 |
| `SMOKE_CHECK_TIMEOUT_SECONDS` | 推荐 `30`；每个真实接口冒烟的总时限，且冒烟不执行生产分页 |
| `OPENREVIEW_VENUES` | 逗号分隔的 venue id |
| `SEMANTIC_SCHOLAR_ENABLED` | 没有获批 Key 时填 `false`；只有同时创建 Key Secret 时才填 `true` |
| `RESEARCH_FEED_URLS` | 已验证的官方 RSS/Atom 地址，逗号分隔 |
| `LESSWRONG_SOURCE_ENABLED` | 可选；默认 `true`，使用公开官方 RSS，无需 Secret |
| `LESSWRONG_KARMA_THRESHOLD` | 可选；默认 `30`，只表示 frontpage Feed 的 karma 入选下限 |
| `ALIGNMENT_FORUM_SOURCE_ENABLED` | 可选；默认 `true`，使用公开官方 RSS |
| `KOL_SOURCE_ENABLED` | 可选；默认 `true`，读取代码内手工核验的个人 Feed |
| `KOL_CUSTOM_FEED_URLS` | 可选自定义 Feed；因身份未内置核验，默认只作二次解读 |
| `KOL_X_SOURCE_ENABLED` | 可选；默认 `true`，但只有配置 X Bearer Token 才读取内置 handle 的近期短文 |
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
| `PARADIGM_RECALL_OVERLAP_DAYS` | 推荐 `30`；只用于 Technical Report、重点研究者和官方研究入口的重叠回补，普通发现仍按 `SOURCING_LOOKBACK_DAYS` |
| `PARADIGM_PRIORITY_AUTHOR_SWEEP_ENABLED` | 推荐 `true`；启用与领域术语独立的重点研究者 arXiv 召回车道 |
| `PARADIGM_MIN_SUBSTANTIVE_DISCUSSIONS` | 默认 `2`；把可核验的非作者实质讨论条数映射到客观 Rubric 选项，不是主观分数 |
| `PARADIGM_MIN_SECONDARY_ENGAGEMENT` | 默认 `50`；未知团队外部承接的互动量客观边界，需结合独立性与平台覆盖解释 |
| `PARADIGM_BOOTSTRAP_LOOKBACK_DAYS` | 推荐 `60`；状态数据库为空或覆盖地图版本变化时，只扩大上述高信号车道 |
| `PARADIGM_RESEARCHER_PROFILE_LIMIT` | 推荐 `6`；兼容性人物档案安全上限，完整作者名单仍保留在证据中 |
| `PARADIGM_KEY_RESEARCHER_LIMIT` | 推荐 `3`；实际核验并写入报告的一作、通讯/资深作者或重点研究者上限 |
| `PARADIGM_*_SAFETY_LIMIT` | 可选运行熔断；默认/推荐 `0`，表示数量完全由 Rubric 结果决定 |
| `PARADIGM_RUN_BUDGET_SECONDS` | 推荐 `3600`；与 1200 秒报告上限合计 80 分钟，给安装、测试、邮件和 artifact 留约 10 分钟；Actions 中不能设为 `0` |
| `PARADIGM_STAGE_RESERVE_SECONDS` | 推荐 `1200`；发现完成后从实际剩余研究预算中为深挖保留的目标余量 |
| `PARADIGM_DISCOVERY_SOURCE_TIMEOUT_SECONDS` | 推荐 `600`；单个并行发现源的墙上时间上限，超时只降级该来源并标记覆盖未闭合 |
| `PARADIGM_REPORT_TIMEOUT_SECONDS` | 推荐 `1200`；研究快照入 outbox 后，研究总编辑渲染的独立上限 |
| `PARADIGM_REPORT_REQUEST_TIMEOUT_SECONDS` | 推荐 `360`；单条路线或轻量总编框架的一次模型请求上限，不再沿用分析请求的 180 秒 |
| `PARADIGM_REPORT_ROUTE_CONCURRENCY` | 推荐 `2`；路线级写作并发，每条通过后立即写入状态 artifact 可恢复的 checkpoint |
| `PARADIGM_REPORT_MAX_RENDER_ATTEMPTS` | 推荐 `3`；持久化渲染重试上限，达到后隔离任务并把候选退回深挖，不影响 SMTP 重试 |
| `PARADIGM_ANALYSIS_BATCH_SIZE` | 推荐 `6`；机制抽取检查点粒度，不是候选上限 |
| `PARADIGM_DEEP_BATCH_SIZE` | 推荐 `1`；深挖检查点粒度，避免半完成档案入库 |
| `EMAIL_MAX_ATTACHMENT_BYTES` | 推荐 `10000000`；报告异常膨胀时阻断发送并保留 outbox |

不配置变量时，自定义研究 RSS 为空，但 LessWrong、Alignment Forum 与内置 KOL Feed 默认启用；研究入口、组织与重点研究者使用仓库中的版本化默认目录，不影响工作流语法。若仓库已有旧版 `PRIORITY_RESEARCH_PAGES` 或 `ESTABLISHED_RESEARCH_ORGANIZATIONS` 长名单，`merge` 会保留它们并同时加载新默认目录，不会冻结后续更新。

静态体检会直接拒绝拼错的布尔值、非整数、越界并发/超时值，以及格式错误的官方页面、Feed、Follow Builders 和 OpenReview venue，而不是沿用默认值继续运行。例如 `flase`、`three-thousand`、`PARADIGM_REPORT_ROUTE_CONCURRENCY=99` 或缺少 `https://` 的页面地址都会在访问状态 artifact 和真实 API 之前失败，并在告警邮件中列出变量名而不回显可能含凭据的完整 URL。

GitHub 上通常只需添加 `TAVILY_API_KEY`；`TAVILY_DISCOVERY_DOMAINS` 留空时同时发现社区和普通技术网页，结果仍只算索引线索。LessWrong/KOL RSS、Rubric、前沿覆盖地图和个人目录都随代码提交，无需创建 Secret 或 Variable。旧版 `PARADIGM_MAX_ANALYSIS_ITEMS`、`PARADIGM_MAX_DEEP_CANDIDATES` 等 Variable 可以删除；即使保留，新代码也不会读取。Reddit 的 Client ID/Secret 必须和“已批准”开关一起配置；只填密钥但不开启批准开关时，代码不会请求 Reddit API。Semantic Scholar 同理：Secret 留空、Variable 为 `false` 时，代码不会匿名请求。

跨提交恢复状态会从新到旧校验不可变快照，跳过下载失败、压缩包损坏、缺少数据库或无法迁移的快照；找到最近一份健康 SQLite 后再迁移到当前 schema 并读取前沿覆盖地图版本。它不再因为 commit SHA 或一个可迁移的版本号变化就重置。普通 Prompt、Skill 或报告样式更新会延续跨周历史；覆盖地图升级时仍恢复旧数据库用于证据去重，但程序会用 60 天窗口补扫新加入的技术面。已经分析过且正文未变化的材料不会再次调用 LLM。报告中每条已通过闸门的路线草稿也会按候选证据与写作 Skill 签名保存；总编超时后，下次只补未完成路线并重做轻量开篇，不重烧完整路线写作。存在活动 outbox 时先恢复它：成功交付后当前进程退出，工作流上传确认状态并自动 dispatch 一个 `reset_state=false`、`smoke_only=false` 的后续 run；确定性输入缺陷或耗尽渲染重试时隔离旧任务，并在当前进程继续新研究。自动 dispatch 失败会让当前任务失败并触发告警，不能静默把续投当成本周研究。若找不到任何健康 artifact，工作流会明确失败并要求人工判断；只有确定要建立新基线时才以 `reset_state=true` 运行。

## 4. 首次手动验收

1. 打开仓库的 `Actions`。
2. 选择 `AI 技术范式雷达`。
3. 点击 `Run workflow`，第一次选择 `7` 天，保持 `smoke_only=true`。精确 arXiv ID 留空，此时 `reset_state` 不影响结果。
4. 确认“配置体检”和“小成本真实接口冒烟”完成，并下载 `paradigm-radar-audit-*` 查看 `smoke_test_latest.json`。文件中的 `contract_version` 可确认线上使用的是哪版探针；`failure_kind=transient_availability` 表示第三方临时限流/超时，显示为 `degraded` 且 Workflow 可通过，但风险不会被隐藏；`multi_endpoint_unavailable` 表示某个多入口能力的有界样本全部失败；配置、真实鉴权或响应契约失败才显示为 `failed`。
5. 再次点击 `Run workflow`，把 `smoke_only` 改成 `false`。已有历史状态或正在修复失败任务时保持 `reset_state=false`，让 schema v6 迁移和 outbox 恢复接管；只有新仓库完全没有可用 artifact、或人工确认要建立全新基线时才使用 `reset_state=true`。
6. 确认“运行离线回归测试”“抓取、分析并发送邮件”“保存跨周去重状态”全部为绿色。
7. 确认收件箱收到邮件及运行审计附件，并在该次运行的 Artifacts 中看到报告、`paradigm-radar-state-<run_id>` 与 `paradigm-radar-audit-*`。状态按运行保存为不可变快照；下一次任务恢复最近一份未过期快照，同时兼容旧的 `paradigm-radar-state` 名称。

首次成功后不需要再保持电脑开机。以后每周五由 GitHub 执行；网页手动运行和定时运行共享同一份去重状态。

## 5. 运维注意事项

- 不要同时长期运行本机 `python main.py --schedule` 或重复的 Codex 自动任务，否则可能在同一天收到两封邮件。
- Actions 显式使用 `submodules: false`：范式流水线读取远程 Feed，不依赖 `gzh_sourcing/we-mp-rss` 或 `social_media_sourcing/follow-builders` 的工作树。`.gitmodules` 仍由离线契约检查 path/URL 完整性，避免历史辅助目录再次让 checkout 因缺失 URL 失败。
- GitHub 定时任务可能因平台负载稍有延迟，所以安排在 09:15 而不是整点。
- 状态和报告 artifact 当前保留 90 天；失败运行只要产生了可校验数据库也会上传，因此报告/SMTP 故障不会迫使下一次重新烧掉整轮研究 tokens。
- 依赖安装、离线回归、配置体检、状态恢复、主流程、状态/报告/审计上传都有稳定 step id。配置在下载或覆盖跨周状态前 fail fast。失败提醒会报告第一处失败而不是笼统归因给 pipeline；审计 artifact 同时保留 `dependencies.log`、`offline_checks.json/log`、`doctor.log` 和状态准备/恢复日志。普通步骤失败时会尽量保留 `current_run.log`、结构化审计、outbox 和包含 Actions 链接的告警。硬取消/Runner 故障仍是平台边界，不能承诺后置步骤执行。
- 三类 artifact 都使用包含 `run_id` 的不可变名称；状态恢复选择最近一份未过期快照。这样新上传失败不会先删除上一份健康状态，重新运行也不会与旧 attempt 争抢同名 artifact。
- 如果连续超过 90 天没有任何可用状态 artifact，下一次生产运行会 fail closed，不会自行当作首跑。人工确认后使用 `reset_state=true` 创建新基线。
- 代码 commit 变化不会自动丢弃旧状态；兼容的数据库 schema 会迁移，已退役的 JSON 字段会忽略，字段类型错误、不可解析 JSON、key/fingerprint 不一致则回退上一份快照。所有快照均损坏或超出兼容范围时任务会失败，只有用户明确选择 `reset_state` 才冷启动。覆盖地图版本变化会保留旧去重历史并扩大为 60 天补扫。V0 阶段确需清空所有历史时才手动勾选 `reset_state`。
- 周报没有合格路线且所有计划材料已完成判断时，仍会成功发送“空雷达”；这是研究结论。若召回覆盖、软预算、人物/一手链接契约或其他 backlog 未闭合，则邮件标题必须标为 `[研究未完成]`，附件只能称为状态/阶段性 memo，不能把尚未分析冒充零创新。
- GitHub 公共仓库连续 60 天无活动可能停用 scheduled workflow，因此本项目建议使用私有仓库。
- 修改工作流后，确保更改已经进入默认分支。
- 工作流使用 Node 24 版本的 `checkout@v6`、`setup-python@v6` 与 `upload-artifact@v7`；任务不执行 git push，因此 checkout 不持久化临时凭据。
