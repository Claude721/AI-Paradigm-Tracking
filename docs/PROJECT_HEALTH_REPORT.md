# 项目工程状态与验收边界

更新：2026-10-14。配置、工作流与当前文档已对齐；本轮静态/离线检查通过，但新模型真实质量与整周交付尚未验收，不能称为稳定生产 V0。

## 当前配置核对

以下是 2026-10-14 对 GitHub 的只读核对及明确配置清理结果，不是模型连通性测试：

| 项目 | 核对结果 |
|---|---|
| SUB | dashscope / qwen3.8-flash，使用用户配置的阿里云 workspace 兼容地址 |
| MAIN | dashscope / qwen3.8-max，同一兼容服务地址，可独立替换 |
| 模型 Secrets | SUB_AGENT_LLM_API_KEY / MAIN_AGENT_LLM_API_KEY 名称存在；旧模型 Secret 不存在，不读回值 |
| 回补窗口 | PARADIGM_RECALL_OVERLAP_DAYS=30 是仍生效的覆盖，保留，不静默缩为默认 14 |
| Tavily 次数保护 | 旧 TAVILY_MAX_REQUESTS_PER_RUN=12 迁为 TAVILY_REQUEST_SAFETY_LIMIT=12，再删除旧名称 |
| 废弃目录参数 | 删除 PRIORITY_RESEARCH_MAX_LINKS_PER_PAGE=4；不映射到不同语义的新参数，实际每入口默认 6 条详情不变 |
| 邮件与其他 Secrets | 保持现有配置，全部名称被正式工作流消费；本轮未发送邮件 |

配置名称存在不证明 Key 有效；历史旧模型 Smoke 不能代替当前 flash/max 与 workspace 地址的验收。独立单路线费用保护仍绑定已核验的 Qwen 3.7 Plus 北京当日契约，不能直接套用给新模型。

## 本轮检查与修正

- AST 对照 config.py 的 178 个配置/常量与生产消费者，计入直接导入及动态角色读取后，无未声明引用或无静态消费者字段。legacy 的字段仍有消费者，不是 V0 必填项，不删除兼容功能。
- .env.example 与配置向导没有重复/未声明字段，移除 V0 模板中的 legacy 信源占位，旧全局模型名称不再作为有效字段出现。
- 补齐文档已声明、但云端未注入的 FOLLOW_BUILDERS_ENABLED/FEED_URL、TAVILY_SOCIAL_MAX_RESULTS、PARADIGM_ALLOW_UPDATES、LOG_LEVEL；现有默认行为不变，未新增对应 Repository Variables。
- 配置状态页优先显示进程环境；即使没有 .env 也能检查。缺失角色明确显示未配置，不把向导示例当运行时 fallback；密钥完全遮蔽，不输出首尾片段。
- 清理不可达旧全局 Key 的脱敏列表项，保留两套新角色 Key 在日志、审计、邮件及持久化错误中的脱敏覆盖。
- 明确 Variables 映射、Workflow inputs、本地 SCHEDULE_*、固定云端邮件策略与高级参数的不同作用面。不把不同预算/窗口/搜索域名当作重复项合并。

本轮 Python 3.11.15 完整离线门 **848 项通过，61.802 秒**，含 5 项配置表面/状态/密钥遮蔽/文档链接回归；标准库通知、无请求导入与 compileall 阶段通过。30 项离线静态配置检查无阻断，3 份 Workflow YAML 解析与 git diff --check 通过。所有测试清除生产配置、忽略真实 .env、拒绝网络，邮件使用模拟。

## 历史真实证据，不冒充新模型认证

- [Run 38418943043 的 Smoke](RUN_38418943043_SMOKE_REVIEW.md)：当时 Qwen 3.7 Plus 最小 JSON 成功、7 类原点能力样本可用；arXiv 429 与官网 403 保留，Tavily/额外平台/SMTP 跳过。它不证明当前两套模型配置。
- [两次 3 天单材料研究](RUN_38500208389_RESEARCH_SAMPLE_REVIEW.md)：共 5 次、54,177 tokens，保守费用 0.532764 元；执行成功，但自动草稿合并不同实验条件、人物例外不合格，人工否决交付。后续修复未追加真实质量复验，不将离线通过写成语义正确。
- [来源 Runner](RUN_38347140838_SOURCE_REVIEW.md)、[NYU/Tavily 子集](RUN_38370843944_INTERFACE_REVIEW.md)、[历史状态迁移](STATE_V9_REPLAY_20261012.md)：分别只证明当时各自协议、授权范围与迁移守恒。Tavily usage 200 不证明搜索已执行；公开官网失败不证明 API Key 失效。

原始 RUN_*、SOURCE_REPAIR_*、STATE_V9_* 记录保留版本、费用和授权边界，不改写为当前使用指南。无需让全部物理入口全绿或历史队列清零，才能研究健康材料。

## 尚未完成的验收

1. 新 MAIN/SUB 的真实连接与 JSON 契约，当前 Key/地址/模型名配对；需明确授权真实调用。
2. 新模型的单路线事实条件、机制解释、独立承接、人物身份/联系检索和非空中文 memo 人工核验；有金额上限的样本先核验对应价格契约。
3. 正式有界计划的耗时与真实用量、总编质量、SMTP 确认、V0 状态保存、同窗恢复/去重与跨周推进。

完整正式运行的时间预算不是人民币硬限额，额外平台与自动预算续跑不能因配置清理自动开启。本轮未运行研究/来源网络验收、未调供应商 API、未发信、未修改生产状态，也未轮换 Secrets。

## 文档同步

按 neat-freak 对根文档、全部 docs 与实际实现进行核对：README 已改为流程、双层模型、最小配置、常用参数、工作流模式及失败恢复指南；配置清单/云端/设计/实施计划/单路线说明同步当前契约。AGENTS.md 链接到 CLAUDE.md，只补独立模型与云端映射两条稳定规则，未新增历史叙事或全局记忆。历史记录无需改动、未删除。

入口：[README](../README.md)、[设计](PARADIGM_RADAR_DESIGN.md)、[配置](CONFIGURATION_CHECKLIST.md)、[云端运维](CLOUD_AUTOMATION.md)、[信源说明](../信源说明.md)。
