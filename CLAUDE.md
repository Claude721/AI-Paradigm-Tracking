# AI Paradigm Tracking 项目规则

## 目标

本项目默认运行 `PIPELINE_MODE=paradigm`：从论文、官方技术博客、原创思想文章、原生代码实现与二次传播中识别正在形成的 AI 技术路线，并把技术推进者及其公开职业联系方式写进研究 memo。

## 不可破坏的边界

- 普通论文通常只产生一个“机制假说”，不能直接等同于一个技术范式；正式 Technical Report 可以拆出多个相互独立、可承接的机制。
- 通过技术硬门槛后仍需核验发布者势能与外部承接：未知团队且缺少实质二次讨论的候选只能留在观察池。
- 作者本人发布工作只能用于身份与背景核验，不能算作独立二次讨论。
- KOL 个人博客和 LessWrong/Alignment Forum 原创文章可以提出 `concept_essay` 机制假说；概念命名、作者身份、论坛精选与 karma 下限均不能自动生成范式，必须有可执行机制或可证伪预测并继续接受外部承接核验。
- 已核验官方 GitHub 组织的新仓库若链接可访问的一手论文/技术页，以外部材料为原点；若技术首次由结构化 README 与代码接口定义，可以形成 `original_implementation` 假说。star/fork 只说明采用势能，不能算独立复现。
- Hacker News、Hugging Face、KOL 二次解读和普通 GitHub Search 只能补充扩散证据，不能单独生成范式。
- LessWrong RSS 的 curated/frontpage/karmaThreshold 只记录选择条件，不得推断或展示帖子精确 karma。
- 论文聚合仓库、日报、awesome list 和同名噪声不得计作实现或复现。
- 最终报告按共享 background 与能力边界组织技术路线，不按论文逐条填表。
- 候选深挖必须形成内部心智模型脚手架：真实对象、训练信号、推理/行动信息流、最小实例、反事实与未闭合接口；它只用于自然写作，不得作为固定字段清单输出。
- 分数只用于内部筛选，不得出现在最终报告中。
- 每条正式路线必须在技术解释中给出简短的“讨论势能判断”，用可核验的二次讨论、独立复现、采用事实和平台覆盖边界解释结论；不得展示内部评分或把作者自发帖、搜索索引命中写成社区承接。
- 技术范式不按周出现；没有新路线时允许空报告，旧路线出现本周新增讨论时可以作为进展更新。
- 首次进入数据库不等于本期新发布；`dateModified` 不能替代 `datePublished`。旧材料只有出现窗口内新增承接、采用或指标量级变化时才能作为 update。
- 报告解释必须使用中文转述，不得输出英文摘要、英文长句或字段拼装降级稿；编辑质量失败必须中止本轮。
- 非空报告中的每条入选路线必须能回到至少一个已核验的一手材料 URL；原文索引由证据对象确定性生成并通过逐路线质量闸门，不得依赖模型记忆、改写或猜测链接。
- 报告中的每一个 HTTP(S) URL 都必须原样来自候选证据、已核验人物档案或确定性索引；即使已经包含一个有效原文链接，任何额外猜测链接也必须阻断交付。
- 带 `Expires`、`Signature` 或 `X-Amz-*` 的临时对象 URL 不得进入报告；抓取重定向后仍须保留稳定 canonical landing/blob URL，持久化报告续投前也要重新验证当前链接契约。
- 人物资料只收集公开职业信息；不得猜测邮箱或把同名研究者强行合并。非空报告中的每位关键人物都必须有最低可核验背景，并完成公开联系方式检索；没有找到时保留检索记录。
- 研究检查点与报告/邮件交付必须解耦：报告或 SMTP 失败不得回滚已经完成的研究；只有交付确认后才能更新 `last_reported_signature`。
- 活动研究批次的窗口、种子和发现/覆盖快照须跨进程保持；任务清单只增不减，历史积压和失败入口不得从完成分母移除。
- 预算自动续跑默认关闭；启用后只能在健康状态已上传、同批次且纯预算延期时按次数/时间/已确认用量上限排队，未知用量或执行/覆盖故障不得自动扩大支出。
- 云端恢复历史交付后必须先保存新状态，再自动排队一个独立完整研究 run；不能在同一 90 分钟进程叠加，也不能让续投静默吞掉本周任务。
- 跨阶段状态必须按无损顺序提交：先保存下游可续跑快照，再把上游对象标记为完成；异常时允许重算，不能形成“上游已完成、下游对象不存在”的永久漏项。
- 召回/队列优先级不等于编辑资格：`origin_priority` 只决定执行顺序，手动 seed 只保证材料可见，二者均不得绕过 Rubric、发布者和外部承接门槛。
- 多来源合并不得降级事实来源：聚合页可以补摘要与指标，但不能覆盖 arXiv/DOI/OpenReview/官方原文 URL、正式报告类型、已核验发布者或重点研究者标记。
- 覆盖地图版本只有在核心领域/学术召回闭合后才能推进；历史漏项可用于一次性复盘，不得成为每周固定 seed、真实日期测试或发布阻断条件。
- 多入口能力不得在 Smoke 或生产包装层退化为首项单点依赖；有界 failover 必须保留入口失败账本，部分失败不能冒充整体鉴权错误。
- 单条原点、候选或历史路线的执行异常必须保留可续跑快照并与预算延期分账，不能终止同轮其他候选或写成技术淘汰。
- 每个批处理阶段必须核对输入/输出身份与基数；模型漏回、返回外来对象或最终 Rubric 未闭合时，只把对应对象保留为 `pending`/`pending_deep`，不能把同批健康对象一起丢弃，也不能生成成功空报告。
- 开启 `EMAIL_PUSH_REQUIRED` 后，邮件发送失败必须让本轮失败，不能提前标记已投递。
- 未经用户明确授权，不运行真实 API 全流程、不发送真实邮件。
- 不提交 `.env`、数据库、日志或 `reports/output/` 中的生成报告。

## 修改研究逻辑时

- 同步检查 `skills/paradigm_extraction/SKILL.md`、`skills/paradigm_synthesis/SKILL.md`、`skills/researcher_trajectory/SKILL.md` 与 `skills/weekly_research_memo/SKILL.md`。
- 结构化字段变化需要同步 `paradigms/models.py`、分析器、报告器和测试。
- 信源变化需要同步 `信源说明.md`；配置变化需要同步 `.env.example` 与 `docs/CONFIGURATION_CHECKLIST.md`。
- GitHub Actions 或邮件语义变化需要同步 `docs/CLOUD_AUTOMATION.md`。

## 本地验证

```bash
python scripts/offline_checks.py
python -m compileall -q agents database notifications paradigms reports scripts skills sources main.py config.py run_audit.py healthcheck.py smokecheck.py
```

`offline_checks.py` 是发布门：它忽略本地 `.env`，清除生产 Secrets/Variables，并把网络代理指向拒绝连接的本地端口。测试不得依赖真实网络、真实密钥或真实邮箱；报告测试使用无网络的模拟编辑客户端。固定日期样例必须注入参考时钟，不能使用墙上当前时间，否则测试会随日历自动过期。

## 文档入口

- `README.md`：运行入口与系统概览
- `docs/PARADIGM_RADAR_DESIGN.md`：当前研究与去重设计
- `docs/CONFIGURATION_CHECKLIST.md`：完整配置清单
- `docs/CLOUD_AUTOMATION.md`：GitHub Actions 与云端邮件
- `docs/PROJECT_HEALTH_REPORT.md`：本轮体检结论
- `信源说明.md`：各信源的作用和限制
