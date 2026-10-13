# V0 工程状态

更新：2026-10-13。默认流程已按有界研究重构；尚不能称为通过真实生产验收的稳定 V0。需要部署这一版本后验证实际模型、报告及重复调度。本轮没有追加真实 API、模型或邮件调用。

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

最终完整门：Python 3.11.15 / 3.12.13 各 **802 项全部通过**，分别 61.270 / 61.441 秒；其中 tests/test_v0_delivery.py 有 22 项默认构造器/交付范围专项回归。标准库故障通知、无请求导入、compileall、3 份 Workflow YAML 解析与 git diff --check 均通过。

发布门清除生产配置并拒绝网络。原始日志为 logs/v0_offline_311_final.log、logs/v0_offline_312_final.log，不提交；测试环境依赖安装不属于研究接口调用。模拟不是实际模型质量、吞吐或提供方稳定性认证。

## 下一次生产验收

部署后确认模型名称/地址/Key 对应，检查已有 Variables 是否覆盖新的 14 天默认。正式运行关闭 source_check_only、smoke_only；第一次 V0 自动从空状态启动。额外平台和自动续跑保持关闭，遵守已有模型费用授权边界。

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

MENTAL_MODEL_WRITING_METHOD.md 和 X_SEARCH_OPTIONS.md 已核对，无需改变技术解释方法或当时授权边界。尚未处理的是线上真实模型/报告/交付验收，不是要求继续修完所有官网。
