# V0 工程状态

更新：2026-10-14。有界流程已发布，Smoke 成功；真实 3 天单材料研究与同材料复测已执行，但人工否决自动草稿的质量，尚不能称为稳定生产 V0。模型连接/JSON 与输入基础能力可用，主要新缺口在人物例外及实验事实边界，不是需要整体重配 API Key。未发送邮件，未恢复/保存生产状态。

## 最新 3 天研究验收

[两轮真实证据与质量复核](RUN_38500208389_RESEARCH_SAMPLE_REVIEW.md)：首轮完成抽取/综合并判 observe；作者漏读修复后，复测生成了中文路线草稿且自动闸门通过。然而草稿合并了不同模型/环境的实验并夸大端到端能力，未核验人物又被普通官方博客的组织例外隐藏，因此人工验收不通过，不交付该草稿。

两轮共 5 次模型调用、54,177 tokens，确认用量的保守估算累计 0.532764 元，低于 20 元授权。署名已正确读取为两人，背景及联系检索未知如实保留。新增人物闸门、正文尾部限制与提示约束修复；没有为这些改动追加第三轮收费研究，因此不能声明新版本的语义质量已真实复验。

最新完整离线发布门：Python 3.11.15 共 833 项通过，59.229 秒，含人物组织例外、末尾限制保留与实验条件提示专项；标准库通知、导入和 compileall 阶段通过。本轮 workflow YAML 解析与 git diff --check 通过。之前两次真实 Runner 的 824 / 828 项不能冒充已测试这轮质量修复，边界分开保留。

## 最新真实 Smoke

[Run 38418943043](RUN_38418943043_SMOKE_REVIEW.md) 在代码 867d2d2 上成功完成：Qwen 一次请求返回完整 JSON（34 tokens），7 类原点召回能力样本可用。arXiv 429 保留为降级，OpenAI 官网 403 后 Anthropic 索引页成功，失败账本保留；Tavily、X、Reddit、Semantic Scholar、SMTP 全部跳过。不是 3 天完整研究，也不是所有官网生产解析验收。

新增 11 项冒烟专项回归；本地 Python 3.11.15 共 813 项通过（58.383 秒），该 Runner Python 3.12.15 共 813 项通过（117.634 秒）。真实模型使用量、测试范围、代码指纹与未验收边界详见上述复核。

## 已确认根因

主要是工程目标错配，不是所有 API Key 都不正常：

- 旧门要求所有物理入口成功、全库 pending 清零，一个官网反爬或无标识书目拖住健康研究。
- 活动批次吸收历史积压并跨周锁定旧窗口，周任务变成无限清偿队列。
- 60 天高信号 bootstrap、年级别书目和无界分页与一小时研究预算不匹配。
- 静态体检只接受 qwen3.7，Actions 硬编码模型/地址，阻碍直接更换 API。
- 补充社区/学术接口未全部成功就拒绝综合，即使已有有效证据。

Runner 有 OpenAlex、GitHub 通过与 Tavily /usage 200 的证据；公开官网 403/429/404、独立费用保护拒绝不证明 Key 失效。见 [来源复核](RUN_38347140838_SOURCE_REVIEW.md)、[授权接口复核](RUN_38370843944_INTERFACE_REVIEW.md)。

## 本轮改变

- 默认交付契约 v2，路线交付与全局队列/所有物理入口解耦，诊断故障真实保留。
- 7 天普通 / 14 天高信号，48 原点、8 深挖、4 历史更新，分页/详情有界且保留各发现车道。
- 本期只包含选定材料和关联路线；下一周归档旧研究计划，旧 pending 不标记成功、不锁住新发现。
- 单个补充平台失败不否决健康证据；全部增强失败、核心判断未完成不能变成成功空报告。
- 坏输入在批次保存前逐条隔离；来源版本、任务回执和路线签名从实际 SQLite 复核。
- 模型/API 可配置，体检不限定 Qwen；OpenAlex/OpenReview 缺配置只降低广度。
- 云端 paradigm-radar-v0-state-* 清洁状态 epoch 不恢复旧失败库，也不删除旧制品。
- 证据、承接、人物、时效、每个 URL、中文编辑和必需 SMTP 门槛保留，不恢复阶段性 memo。

## 离线证据

新增真实默认构造器与 SQLite 的端到端回归：

- 官网失败 + 历史 pending：完成本期判断可以生成范围明确的空报告。
- 单个模型漏回 + 健康路线 + Tavily 故障：健康路线进入 outbox，坏对象仍 pending。
- 非空中文报告 → 确定性人物/原文索引 → 模拟 SMTP 确认 → 去重签名登记。
- 没有新论文的一周，旧路线本期新增独立采用仍可作为进展更新；部分平台失败不在下次运行破坏已核验路线。
- 原点与选定范围原子提交/回滚，深挖计划保持有界；未选假说保留，不写成研究淘汰。
- 来源不足、模型漏回全部、坏输入导致空结果：拒绝成功空报告。
- 篡改统计、候选签名或来源版本：持久化闸门拒绝。
- 下一周正常推进，旧计划归档但任务未虚报完成。

首次重构完整门：Python 3.11.15 / 3.12.13 各 **802 项全部通过**，分别 61.270 / 61.441 秒；其中 tests/test_v0_delivery.py 有 22 项默认构造器/交付范围专项回归。标准库故障通知、无请求导入、compileall、3 份 Workflow YAML 解析与 git diff --check 均通过。当前包含冒烟专项的 813 项证据见上节。

发布门清除生产配置并拒绝网络。原始日志为 logs/v0_offline_311_final.log、logs/v0_offline_312_final.log，不提交；测试环境依赖安装不属于研究接口调用。模拟不是实际模型质量、吞吐或提供方稳定性认证。

## 下一次生产验收

最小 JSON 已确认当前模型名称/地址/Key 对应；云端 PARADIGM_RECALL_OVERLAP_DAYS=30 仍覆盖新的 14 天默认，未擅自修改。完整正式运行关闭 source_check_only、smoke_only；第一次 V0 自动从空状态启动。下一层小样本须独立状态、不发信、显式费用边界，不直接把普通生产 workflow 当成低费用测试。额外平台和自动续跑保持关闭。

验收有界计划耗时、路线事实/链接、中文质量、人物归因、实际 tokens、邮件确认、V0 状态保存，以及同窗/跨周去重。不再等待每个官网诊断全绿。若模型持续截断/结构失败，优先更换模型或兼容接口配置，不继续堆叠覆盖工程。

历史容量、解析和状态迁移证据保留在 RUN_*、SOURCE_REPAIR_*、STATE_V9_* 文档；当时的全入口交付要求已被本次用户澄清取代。

## 文档与规则同步清单

neat-freak 文档同步核对后就地精简当前入口，历史记录加版本边界，不改写原观测/费用/授权。项目没有独立记忆库，AGENTS.md 链接到 CLAUDE.md；未修改全局规则。

| 位置 | 实际变更 |
|---|---|
| 根文档/配置 | README.md、CLAUDE.md、信源说明.md、.env.example、setup_env.py：默认有界流程、模型继承与新参数 |
| 当前设计/运维 | PARADIGM_RADAR_DESIGN.md、CONFIGURATION_CHECKLIST.md、CLOUD_AUTOMATION.md、PROJECT_HEALTH_REPORT.md、V0_REBUILD_PLAN.md：按代码重写，真实验收与离线证明分开 |
| 诊断/名单/独立测试 | SOURCE_ACCEPTANCE.md、SOURCE_API_KEY_VERIFICATION.md、FRONTIER_RESEARCH_WATCHLIST.md、ONE_ROUTE_ACCEPTANCE.md：来源缺口非全局门、接口证据/费用授权与通用模型区分 |
| 历史记录 | RUN_37899070924_CAPACITY_REVIEW.md、RUN_38027283680_SOURCE_REVIEW.md、RUN_38232622479_SOURCE_REVIEW.md、RUN_38235929382_MODEL_REVIEW.md、RUN_38238493094_MODEL_REVIEW.md、RUN_38239955723_SOURCE_REVIEW.md、RUN_38342316068_SOURCE_REVIEW.md、RUN_38347140838_SOURCE_REVIEW.md、RUN_38370843944_INTERFACE_REVIEW.md、SOURCE_ACCEPTANCE_20261010.md、INPUT_CLOSURE_REPAIR_20261012.md、SOURCE_REPAIR_20261013.md、STATE_V9_REPLAY_20261012.md：旧全入口要求标记为已被取代 |
| 研究提示 | paradigm_extraction、paradigm_synthesis、researcher_trajectory、weekly_research_memo、weekly_memo_frame 的 SKILL.md：自身路线核验与其他来源故障解耦，禁止阶段性输出 |

MENTAL_MODEL_WRITING_METHOD.md 和 X_SEARCH_OPTIONS.md 已核对，无需改变技术解释方法或当时授权边界。真实最小模型 JSON / 输入冒烟已闭合；尚未处理的是线上真实研究/报告/交付验收，不是要求继续修完所有官网。
