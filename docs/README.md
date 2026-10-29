# 文档索引

更新：2026-10-29。日常部署按下列当前指南执行。历史记录保存当时的代码、配置、授权、价格与实测结果，不构成当前的调用许可或验收标准；特别是旧版“全部完成/尝试才交付”和人物硬门槛已经失效。原文署名独立于可选人物履历增强展示；本次线上历史状态清理及下一次冷启动边界见[云端运维](CLOUD_AUTOMATION.md#显式清空线上状态)。

## 当前操作与设计

| 文档 | 用途 |
|---|---|
| [项目入口](../README.md) | 流程概览、本地与 Actions 入口 |
| [项目规则](../CLAUDE.md) | 修改代码时不可破坏的约束；AGENTS.md 指向同一文件 |
| [研究设计](PARADIGM_RADAR_DESIGN.md) | 机制、去重、承接、逐路线质量门及 v5 交付 |
| [配置清单](CONFIGURATION_CHECKLIST.md) | 双层模型、信源、窗口、费用与云端映射 |
| [云端运行](CLOUD_AUTOMATION.md) | Workflow、状态恢复、邮件、制品与诊断 |
| [工程状态](PROJECT_HEALTH_REPORT.md) | 当前修复证据、已知缺口与下一步真实验收 |
| [V0 验收计划](V0_REBUILD_PLAN.md) | 尚需的样本、整周、恢复与跨周验收 |
| [信源说明](../信源说明.md) | 各类输入的角色、访问限制和内容清理 |
| [信源验收](SOURCE_ACCEPTANCE.md) | 逐入口检查方法；文内旧运行表只代表当时结果 |
| [API Key 复验](SOURCE_API_KEY_VERIFICATION.md) | 区分注入、鉴权、额度、反爬和解析故障 |
| [单路线验收](ONE_ROUTE_ACCEPTANCE.md) | 当次授权、配置驱动价格、有界真实研究；历史报价不得直接复用 |
| [私有 JSON 留存](RESEARCH_ARCHIVE.md) | 邮件 ZIP、本地合并、模型判断与人工反馈 |
| [心智模型写作](MENTAL_MODEL_WRITING_METHOD.md) | 技术解释方法，不是最终报告固定字段模板 |
| [研究覆盖目录](FRONTIER_RESEARCH_WATCHLIST.md) | 重点团队/研究者目录的用途、版本与覆盖边界 |

## 历史记录

这些文档用于复盘，不据其中的“当前/最新”字样推断今天的生产状态。修改研究规则应同步当前指南，保留历史实测事实；当前运行状态以工程状态、执行 SHA 和本轮审计为准。

| 记录 | 范围 |
|---|---|
| [上下文与流程优化](CONTEXT_FLOW_OPTIMIZATION.md) | 2026-10-18 提示词字符回放及验证边界 |
| [输入闭合修复](INPUT_CLOSURE_REPAIR_20261012.md) | 2026-10-12 输入协议与覆盖修复 |
| [模型验收](MODEL_ACCEPTANCE_20261014.md) | 2026-10-14 模型/价格/质量证据 |
| [初期信源验收](SOURCE_ACCEPTANCE_20261010.md) | 2026-10-10 逐入口结果 |
| [信源修复](SOURCE_REPAIR_20261013.md) | 2026-10-13 入口适配与限制 |
| [v9 状态回放](STATE_V9_REPLAY_20261012.md) | 2026-10-12 历史状态契约 |
| [X 搜索选项](X_SEARCH_OPTIONS.md) | 当时接口/替代方案调研，不代表当前价格或权限 |
| [Run 37899070924](RUN_37899070924_CAPACITY_REVIEW.md) | 容量与历史积压 |
| [Run 38027283680](RUN_38027283680_SOURCE_REVIEW.md) | 信源检查与 Key 诊断 |
| [Run 38232622479](RUN_38232622479_SOURCE_REVIEW.md) | 信源复验 |
| [Run 38235929382](RUN_38235929382_MODEL_REVIEW.md) | 首轮有界模型验收 |
| [Run 38238493094](RUN_38238493094_MODEL_REVIEW.md) | 同材料模型复测 |
| [Run 38239955723](RUN_38239955723_SOURCE_REVIEW.md) | 信源运行复核 |
| [Run 38342316068](RUN_38342316068_SOURCE_REVIEW.md) | Runner 入口复核 |
| [Run 38347140838](RUN_38347140838_SOURCE_REVIEW.md) | 离线 fixture 污染与信源结果 |
| [Run 38370843944](RUN_38370843944_INTERFACE_REVIEW.md) | NYU/Tavily 有界接口验收 |
| [Run 38418943043](RUN_38418943043_SMOKE_REVIEW.md) | 双层模型 Smoke |
| [Run 38500208389](RUN_38500208389_RESEARCH_SAMPLE_REVIEW.md) | 真实研究小样本 |
| [Run 38566679912](RUN_38566679912_REPAIR_REVIEW.md) | 首轮正式交付与修复 |
| [Run 38696723495](RUN_38696723495_REPAIR_REVIEW.md) | 平台闭合、类型及材料取舍 |
| [Run 38706808763](RUN_38706808763_EXECUTION_REVIEW.md) | 单条失败与旧交付契约 |
| [Run 38858172858](RUN_38858172858_REPAIR_REVIEW.md) | 来源关联、执行顺序和吞吐 |
| [Run 39012958111](RUN_39012958111_WEEKLY_DELIVERY_REVIEW.md) | v5 周交付优先契约 |

源码或提示词修改后重新执行离线发布门；历史测试数量不能认证新源码。真实模型/搜索/邮件验收需当次明确授权，不因读到历史命令而自动执行。
