# AI 技术范式雷达体检报告

> 2026-07-26 第五次更新。本轮从本地 `.env` 出发完成小成本真实接口冒烟、定向修复和离线回归；没有运行真实完整流水线，也没有发送邮件。

## 当前结论

发现、分析、跨周去重、云端定时和 SMTP 投递主链路已经跑通。第三轮排查进一步定位到四个运行级根因：Semantic Scholar 在无 Key 时仍匿名并发调用；OpenReview 批量 Notes 端点触发浏览器 Challenge；OpenAlex 错误排序/宽查询召回无关论文；候选数量直接放大 Qwen、GitHub 与 Tavily 调用，且缺少可审计漏斗。

本轮把主链路进一步调整为“高优先级原点召回 → 多机制假说 → 技术/发布者/外部承接联合准入 → 历史路线刷新 → 人物多源核验 → 研究总编辑 Memo”：

- arXiv 增加可独立降级的 Technical Report 专项查询；Moonshot、OpenAI、Anthropic、DeepMind、Meta AI、Qwen 等无 RSS 官方研究入口进入高优先级召回，官方页面直接链接的 PDF 报告也会提取正文。
- 正式 Technical Report 最多拆出六个可独立承接的机制，避免只用一个模型总标签吞掉注意力、路由、数据或学习范式创新。
- 抽取与综合 Agent 必须区分论文的宏大 background 和实际 intervention；实际只做局部模块时会下调外延与扎实度，不能沿用作者营销措辞。
- 综合 Agent 审计每条外部证据，排除论文聚合仓库和同名误匹配，并提炼二次讨论真正关注的亮点、争议与用途。
- 发布者与社区势能成为准入门槛：前沿组织正式 Technical Report/官方研究发布优先；普通论文即使来自强团队也需外部响应；未知团队必须有跨来源讨论、独立承接或高关注研究者解读。
- 可选 X 标题搜索用于找作者本人账号、公开简介与 KOL 解读。作者本人发帖只核验身份，不能算独立二次讨论。
- Tavily 以一次低成本组合查询发现公开索引的 X、Reddit、小红书页面，但不参与趋势准入或声量评分；获批的 Reddit OAuth 负责精确搜索及帖子分数、评论量，未配置时会明确保留覆盖缺口。
- 社区用户正文只供当轮综合与作者身份对齐；分析完成即清除，持久化和邮件只保留原链接、聚合指标、覆盖状态与中文提炼。
- 观察池和已报告路线每周重新检索近期讨论；旧范式出现新复现、新解读时可作为“进展更新”，没有合格内容时允许空报告。
- 人物增强在 Semantic Scholar 之外增加 OpenAlex、ORCID 与已核验个人主页，记录机构、代表作、公开链接、邮箱和完整检索轨迹。
- 最终报告的英文长段、评分、表格和人物漏写由确定性质量闸门检查；失败后调用终审 Skill 全文重写一次，仍失败就停止任务，不再输出字段拼装降级稿，也不会发送邮件。
- 前沿发布者目录升级为 97 个组织/具名实验室、70 位重点研究者和 33 个官方研究入口。主动召回、已建立组织、监测组织与重点研究者四层分离；新增中国大厂与模型厂商、海外 World Model/机器人组织、清华/北大及海外具名实验室。
- 机构匹配从双向子串改为完整字段/分段精确匹配，整所大学和 `Seed`、`GLM`、`AI Lab` 等歧义裸词不会自动加权。重点研究者必须再由公开主页或学术 ID 核验，并且只贡献 `verified` 线索。
- GitHub Variables 默认采用 `merge`：旧自定义项会保留，但不再覆盖和冻结仓库的新版内置目录。用户追加的研究页面没有 owner 元数据，只允许同域抓取并降级为 `verified`，不能依赖网页自报站点名冒充知名机构。
- Semantic Scholar 改为“双开关”：只有 `SEMANTIC_SCHOLAR_ENABLED=true` 且存在获批 Key 时才请求；本地无学术邮箱时完整跳过，不再匿名消耗共享额度。
- OpenReview 改用公开 `/notes/search` 主题检索并按 venue 过滤，已定向返回 `Ctrl-World` 等相关工作；OpenAlex 改为结构化查询、相关性优先和日期次优先，已定向返回 VLA/embodied AI 相关论文。
- GitHub Search 没有 Token 时不匿名请求；出现 401/403/429 后本轮熔断。Tavily 增加整轮 hard cap，默认最多 12 个 basic request。
- 原始材料最多分析 30 条、路线最多深挖 16 条。超出预算的原点和路线进入持久化 FIFO 队列，即使下一周离开 7 天召回窗口也不会丢失。
- 每轮生成结构化审计与独立本轮日志，包含信源返回量、材料/路线去留理由和模型各阶段 token 用量；不保存 prompt、模型正文或私有推理。成功时随邮件发送，云端失败时也作为 artifact 保留。

## 可靠性边界

- 单一论文、博客、OpenReview、Semantic Scholar、GitHub、HN 或 Follow Builders 信源失败时继续降级运行；真实 smoke test 则会对“已经配置却鉴权失败”的接口返回非零，避免把降级误当成健康。
- 必需邮件投递失败会让任务失败；失败任务不登记交付，并回滚本次数据库变化，修复后可完整重试。
- GitHub Actions 只在任务成功后保存去重数据库，避免云端失败状态污染下一周。
- 报告编辑质量失败与 SMTP 失败一样会触发整轮回滚，可在修复后安全重跑。
- GitHub 搜索先做代码仓库启发式过滤，再由综合 Agent 审计；宁可漏掉弱信号，也不把论文日报当复现。
- 联系方式只收录公开专业来源，不猜测邮箱，不抓取私人信息。LinkedIn 只在 ORCID 或已核验个人主页明确链接时收录。

## 验证结果

- Python 静态编译通过。
- 49 项本地单元测试通过，新增覆盖 Semantic Scholar 零匿名请求、OpenReview 搜索端点、OpenAlex 相关性排序、Tavily 整轮预算、Hugging Face PDF 下载、未分析原点/待深挖路线持久化、运行审计和邮件审计附件。
- GitHub Actions YAML 语法通过本地解析。
- 工作流已升级为 Node 24 Actions：`checkout@v6`、`setup-python@v6`、`upload-artifact@v7`；关闭 checkout 凭据持久化以消除无用的 post-job Git 清理。
- 真实小成本验证已通过 Qwen `qwen3.7-plus`、arXiv、Hugging Face Daily Papers、OpenAlex Works/Authors、官方研究页、Hacker News、Tavily 和 QQ SMTP 登录（未发信）。OpenReview 在修复后通过定向复测。Semantic Scholar、Reddit 与 X 因未配置而按设计跳过。
- 当前唯一已知的本地凭据阻塞是 `GITHUB_TOKEN`：GitHub `/rate_limit` 明确返回 HTTP 401；Token 长度、前缀和空白格式正常，因此需要在 GitHub 重新生成后替换，而不是继续改代码。

下一次真实验收先替换本地 GitHub Token，再运行 `python main.py --smoke-test`。全部已配置接口通过后，才执行一次受预算保护的完整流水线，重点检查重要 Technical Report 是否进入候选、审计漏斗能否解释每次去留、人物公开入口是否充分，以及正文是否完全摆脱英文摘要直出。
