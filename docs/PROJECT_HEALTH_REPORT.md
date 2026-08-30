# AI 技术范式雷达｜V0 工程体检

> 更新日期：2026-08-30
> 结论：三次 GitHub Actions 失败暴露的 outbox 队首阻塞，以及首次正式 V0 验收暴露的证据持久化契约缺口均已修复；代码与离线发布门达到 **V0 候选基线**。GitHub Smoke 已通过，仍需一次保留历史状态的正常周窗运行完成线上验收，当前不能把离线通过写成生产已稳定。

## 当前架构结论

项目的主干已经稳定为“多车道召回 → 机制级 Rubric → 外部承接与人物核验 → 技术心智模型 → 中文研究 Memo → durable outbox → 邮件”的闭环。候选数量不使用固定 Top-K，技术去留由版本化 Rubric 决定；运行时间预算只控制 backlog 处理进度。一手原点统一为论文/Technical Report、官方技术页、原创思想文章和原生代码实现；任何一份材料都只能先提出机制假说，不能凭作者名气、Feed 精选或仓库热度自动成为范式。

## 2026-08 三次 Actions 失败复盘

附件 `logs_86623174249.zip`、`logs_87943037625.zip`、`logs_89898388408.zip` 对应 8 月 16、21、28 日的三次失败。第一轮完整研究运行约 63 分钟：召回 2770 个原点，分析后形成 8 个候选，其中 1 条进入报告；流水线最后在报告终检抛出 `ValueError: 关键人物覆盖不足`。后两轮都恢复同一个 delivery key `734c9527f55c…`，约 2 分钟内在相同位置失败，没有进入新研究。由此可以排除“Secrets 未配置”是主因，确认是持久化 outbox 的 poison-message/head-of-line blocking。

直接根因是路线草稿与全文终检采用了两套不一致的归因契约：路线级检查允许正文写已核验发布组织，全文检查在候选存在人物档案时却只认人物姓名；而确定性人物索引又在全文检查之后才追加。日志中的路线以组织归因通过草稿闸门，却在每次最终检查中失败。修复后，组织/人物归因规则统一，人物与原文确定性索引在检查最终制品前装配，冻结快照无需依靠模型第二次“碰巧写出人名”。

仅修这一行仍不足以达到可运维 V0，因此同时加入 poison-message 处置：schema v6 持久化渲染尝试次数与失败类型；确定性输入缺陷、以及内置修订后仍失败的最终质量契约立即隔离，网络/超时等其他渲染失败默认 3 次后隔离；候选与证据在同一事务中退回 `pending_deep`，隔离记录保留审计但不再占住活动队首。状态恢复会主动检查旧 outbox 的当前输入契约，工作流恢复后执行只读 `--inspect-state`。SMTP 失败仍保留已验证报告并遵守 `EMAIL_PUSH_REQUIRED`，没有被隔离机制弱化。

## 2026-08-30 正式运行失败复盘

`logs_90220369395.zip` 对应的 Smoke 已成功，随后正式运行恢复 schema v6 健康状态：160 条路线、5324 条证据、8 次交付，活动 outbox、隔离任务和待处理项均为 0。生产发现取得 2785 个原点和 333 条支持证据，但在发现检查点之后、进入模型分析之前失败：`load_pending_origins()` 反序列化一条证据时抛出 `ValueError: 证据状态字段 published_at 必须是字符串`。状态准备使用相同的领域校验再次拒绝该数据库，因此损坏状态没有覆盖上一份健康 artifact；旧 outbox 修复并未回归，Secrets 也不是本次根因。

最高概率输入是 OpenAlex 响应中的 `publication_date: null`。适配器过去使用 `get("publication_date", "")`，字段存在但值为 JSON null 时仍会得到 Python `None`；Hugging Face、Follow Builders 等其他可空日期入口也有相同形态。领域对象在首次写库时没有做完整回环校验，导致本应在来源边界消化的可空值先进入 SQLite，再在恢复边界被更严格的校验拒绝。Smoke 使用独立最小请求，只验证连通性、鉴权和少量响应字段，不运行生产适配器、发现合并、SQLite 检查点或序列化回环，因此 Smoke 成功与这次正式失败并不矛盾。

修复后，上游可空标量在领域边界统一规范化；证据写库前逐条执行与恢复路径相同的 canonical round-trip 校验。结构仍不合法的单条记录按来源计数并隔离，健康同批记录继续落库；拒绝计数进入 `run_incomplete` 和 pending 账本，核心原点拒绝还会阻止覆盖地图版本推进，避免形成“成功空报告”或漏扫基线。下一次线上验收应保持 `reset_state=false`，继续恢复上一份健康 artifact，不需要清库。

这版 V0 重点收口了此前完整运行、思想源扩展、四次 Actions 日志与逐分支故障注入暴露出的十四类系统性问题：

1. **旧材料被误写成本周新技术。** 清空数据库只改变 `first_seen`，不再改变发布时间语义。报告前新增独立时效闸门：窗口内有可核验一手发布日期才是 `recent_primary`；旧路线只有本期新增承接、采用或指标量级变化时才作为 `historical_reactivated/update`。官方页面的 `dateModified` 与 `datePublished` 分栏，不能互相替代。
2. **报告带入会过期或猜测的链接。** 高优先级页面始终保存索引发现的 canonical URL，不保存重定向后的签名 CDN 地址；Hugging Face `resolve` 被规范为稳定 `blob` 页面，带 `Expires`、`Signature` 或 `X-Amz-*` 的对象 URL 直接拒绝。报告正文、写作 dossier、质量闸门和确定性原文索引统一使用同一 URL 规范。现在不仅要求“至少一个原文”，还逐一核对全文所有 URL 与证据/人物档案，额外猜测链接和擅自改变路径大小写都会阻断交付；中文标点相邻的 Markdown 链接有专门解析回归。持久化报告续投前也会重新执行当前质量契约，旧制品不能绕过修复。
3. **官方 GitHub 首发在候选形成前不可见。** 已核验官方组织发布事件车道按组织身份和仓库 `created_at` 召回，不依赖不完整的项目关键词库；每个组织按创建时间分页到任务窗口边界，不再静默截断最新 30 个仓库，分页中断会进入覆盖审计。外链 arXiv/DOI/官方技术页经当轮 HTTP 核验后成为原点；没有外链时，只有结构化 README 同时给出 AI 对象、多个训练/运行机制与可审计接口，才形成 `original_implementation` 假说。SDK、demo、awesome/论文列表和短 README 被确定性排除；仓库日期不覆盖外部材料日期，stars/forks 只说明采用势能，不算独立复现。
4. **历史交付续投吞掉周五新研究。** 单个 Python 进程仍只做“恢复交付”或“完整研究”之一，避免 90 分钟 job 叠加两个长任务。恢复成功后会写入无敏感信息的 `pipeline_result.json`；GitHub Actions 先上传带交付确认的新状态，再用 `actions:write` 自动 dispatch 一个 `reset_state=false` 的完整研究 run。dispatch 失败有独立步骤归因、日志与失败邮件，不会静默成功。
5. **思想源不能只靠关键词搜索，也不能把高信号论坛变成新噪声源。** 新增 LessWrong/Alignment Forum 官方 RSS、37 人手工 KOL 目录、19 个实测个人 Feed 与可选的已知 X 账号车道。首版真实样本曾把政策讨论、个人感想和普通发布日志一起放行，已收紧为标题/正文共同满足 AI 技术对象与 intervention，排除周记、政策、招聘和发布日志。`concept_origin` 与 `secondary_only` 分账，论坛 karma 只保存阈值下限，同一 LessWrong/Alignment Forum cross-post 使用稳定帖子 ID 去重。
6. **模型少回一条会形成永久漏项。** 机制抽取、证据增强、范式综合、人物分析和历史刷新均校验输入/输出身份与基数；漏回、重复外来对象或非列表输出会保留对应对象待重试，不能标记上游完成。最终 Rubric 未闭合统一回到 `pending_deep` 并计入 `run_incomplete`，不再落入历史刷新后生成“成功但 0 条”的假结论。
7. **一个坏样本拖垮整批。** 批次发生非超时结构异常时递归二分，直到隔离单条；健康同批对象仍可提交。预算超时与执行异常分账，前者不增加技术失败计数，后者保留原快照和失败时间。
8. **有界历史刷新重复处理队首。** schema v5 增加 `last_refresh_attempt_at`，从最久未尝试路线轮转。刷新成功但无新证据也会持久化并清除旧执行失败，既避免队尾饿死，也避免偶发故障永久降权。
9. **SQLite 页完整但领域 JSON 已损坏。** 状态恢复除 `quick_check`、表/列迁移外，逐行反序列化证据、候选、outbox 与交付历史并核对 key/fingerprint。错误类型、不可解析 JSON 或键不一致会拒绝该快照并自动尝试更早的不可变 artifact；已退役字段仍可向后兼容忽略。
10. **外部 API 的脏数字与安全上限造成批量故障/静默截断。** `N/A`、`NaN`、无穷值、错误 JSON 类型统一按不可用指标处理，不再中断 Rubric 或整个信源。发现 safety limit 只停止尚未执行的传输车道，已取得的跨源材料不再二次切片；官方 GitHub 任一窗口内分页失败都标为 `partial`，不按组织数量占比淡化。
11. **报告坏快照永久占住 outbox。** 活动状态与 `quarantined` 分离，渲染次数跨进程持久化；确定性输入缺陷与耗尽重试的制品不再阻塞新研究，候选退回深挖而不是被删除或误写成技术淘汰。
12. **路线归因规则前后不一致。** 路线草稿和全文都接受“已核验关键人物或已核验发布组织”，确定性人物/原文索引在最终质量检查前装配。新增回归直接模拟三次 Actions 的“正文只有组织、候选已有核验人物”形态。
13. **arXiv 项目链接保留 TeX 转义。** `paper\_files` 过去会变成 `%5C` 后请求不存在的 NeurIPS 页面；现在只解开已知安全的 TeX 标点转义，仍含未知反斜杠的链接拒绝请求。Arc Institute 与 Isomorphic Labs 的长期 404 聚合根入口也已更新为当前官方 News 索引。
14. **外部可空字段先写后验造成 SQLite 自污染。** OpenAlex 等接口可能显式返回 JSON null；现在日期及其他可空领域标量在来源边界规范化，写库前逐条执行和恢复路径相同的结构校验。坏记录只隔离自身并计入未完成账本，健康同批记录继续提交；核心原点被拒绝时覆盖地图基线不会推进。

## 召回与报告边界

- 普通 arXiv 地景、OpenAlex、OpenReview、Hugging Face 与可选 X Recent Search 使用周窗；正式报告、重点研究者、官方页面、官方仓库、LessWrong/Alignment Forum 与 KOL Feed 使用重叠回补窗。reset 或覆盖地图升级只扩大高信号车道，不放大全部宽关键词。
- 官方模型发布能够被召回和审计，但没有 Technical Report、可独立机制或外部承接时可以正常停在观察池。品牌、star、作者自述和 changelog 本身都不自动放行。
- 旧技术若在本周被重新采用或讨论，可以作为进展更新；报告必须区分“原技术何时发布”和“本周新增了什么”，不能借当下热门主题重写历史。
- 每条非空路线必须有安全、稳定、可点击的一手材料 URL，完成关键人物最低背景与公开职业联系方式检索，并自然给出讨论势能的结论、证据与覆盖边界。
- Tavily 只发现公开索引线索；作者原帖是提出行为，不是自己的二次承接；论坛精选、karma 下限和官方 GitHub stars/forks 都与独立复现、非作者分析、后续论文及产品采用分账。

## 工程可靠性

- 发现结果、候选、路线草稿、渲染报告和交付确认分层检查点化。报告或 SMTP 失败不会回滚研究；只有交付确认后才更新 `last_reported_signature`。
- 证据检查点先在事务外逐条完成 canonical round-trip；任何结构错误只拒绝对应记录并记录来源计数，不会把同批健康对象一并回滚，也不会把带坏 payload 的数据库上传为新状态。
- 状态 artifact 使用不可变 `run_id` 名称，恢复时从新到旧执行 schema v6 的 SQLite、领域 payload 与活动 outbox 输入契约校验并迁移；找不到健康状态时 fail closed，只有显式 `reset_state=true` 才冷启动。
- 单个发现源超时、部分失败、429、运行预算耗尽与 Rubric 淘汰分开审计。存在 backlog 或覆盖缺口时，0 条交付只能称为研究未完成，不能写成本周没有新技术。
- 多路线写作按路线保存 fragment；Skill 或证据变化会改变 fragment key。已渲染报告在发送前重新验证，避免历史制品携带旧链接或旧编辑规则。
- 失败提醒能够区分依赖、离线回归、配置、状态恢复、研究、artifact 上传和“排队本周新研究”步骤。Runner 被平台直接取消或硬超时仍是 GitHub Actions 的不可消除边界。
- Secret 不写入日志或审计；社区用户正文当轮提炼后清除；人物只使用公开职业信息，不猜测邮箱或合并同名研究者。

## 本地发布门

2026-08-30 使用 `venv/bin/python scripts/offline_checks.py` 完成 hermetic 发布门：

- **253 项单元测试通过**；
- 测试子进程清空生产密钥和可变研究配置，并把 HTTP 代理指向拒绝端口，确认测试不依赖真实网络、邮箱或 `.env`；
- stdlib 模式下失败提醒可导入；手工探针模块只导入不发请求；
- `compileall` 通过；
- 工作流契约测试覆盖 Node 24 Actions、不可变状态、恢复后状态检查、自动 dispatch、失败归因和审计 artifact；
- `git diff --check` 通过。

本轮随后执行本地 `python main.py --doctor` 静态体检；它只检查配置契约，不请求真实服务。项目内 5 个研究 Skill 另行通过 `skill-creator` 的 `quick_validate.py` 格式校验。上一轮在 2026-08-15 对公开思想源做过只读端点验收：LessWrong 两个官方 Feed 与 Alignment Forum Feed **3/3 成功**，19 个手工核验 KOL Feed **19/19 成功**；这只是当时的网络快照，不替代每次 GitHub Smoke 的实时健康检查。本轮没有重复调用真实信源。

新增回归覆盖：三次日志对应的组织归因/人物终检不一致、旧状态输入契约迁移、渲染尝试耗尽后无损隔离、隔离恢复后继续新研究、本轮新报告被隔离时保持 Workflow 失败、TeX 转义项目 URL、当前 Arc/Isomorphic 官方入口、工作流状态可观测性、OpenAlex null 发布日期的来源到 SQLite 回环，以及同一证据检查点中坏记录与健康记录的事务前隔离；同时保留此前的模型基数、批次隔离、日期、来源权威性、思想源、人物、URL 与续投契约。

## 上线验收与剩余边界

本轮遵守项目边界，**没有运行真实全流程、没有发送邮件**。代码可作为 V0 候选基线提交，但真实服务的限流、站点 DOM、GitHub 组织更名和模型输出质量只能在线验证，不能由离线测试证明。

提交默认分支后按以下顺序验收：

1. 已完成的 `smoke_only=true` 只证明 Qwen、arXiv、HF、OpenAlex、GitHub、官方页面、LessWrong/KOL Feed、Tavily 与 SMTP 的最小能力符合当前配置；它不证明生产解析器或 SQLite 回环正确。
2. 直接运行 `smoke_only=false`、`reset_state=false`：恢复上一份健康 schema v6 artifact，并验证发现、来源规范化、证据检查点与后续分析能越过本次失败位置。不得为了绕过该错误使用 `reset_state=true`。
3. 下载审计 artifact，确认 `evidence_checkpoint_rejected_count` 为 0；若非 0，任务必须显示研究未完成并给出按来源计数，而健康记录仍应存在于状态。继续检查 `official_repository_release_coverage`、`report_freshness`、`pipeline_result.json`、逐官方页面健康度和最终原文索引。报告链接应全部是稳定 landing/blob/论文页，不得出现带签名 CDN 查询参数。

这次不需要新增 Secrets 或 Variables：LessWrong、Alignment Forum 与 KOL Feed 均为公开 RSS，工作流已有默认值。若未来希望读取已整理的 KOL X 账号，才需要保留已有的可选 `TWITTER_BEARER_TOKEN`，并由实际 Smoke 验证 X Recent Search 计划权限。官方仓库发现复用工作流内置 `GITHUB_TOKEN`；自动 continuation 使用同一临时 token 的 `actions:write`，仓库内容权限仍为只读。
