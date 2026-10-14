# 项目工程状态与验收边界

更新：2026-10-14。配置驱动的 Flash/Max 已通过 Runner 冒烟和一次真实观察判断；本轮继续修复原文传递/模型事实归因并验收。非空写作与整周交付尚未验收，不能称为稳定生产 V0。

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

配置名称存在不证明 Key 有效；历史旧模型 Smoke 不能代替当前 flash/max 与 workspace 地址的验收。本轮移除旧模型/地址/代码日期绑定：调用协议按提供方与可选角色请求选项配置；单路线费用从与真实角色匹配的已核验价格数据读取，不套用旧价格。

## 本轮检查与修正

- AST 对照 config.py 配置与生产消费者，计入直接导入及动态角色读取后，无未声明引用或无静态消费者字段。legacy 的字段仍有消费者，不是 V0 必填项，不删除兼容功能。
- .env.example 与配置向导没有重复/未声明字段，移除 V0 模板中的 legacy 信源占位，旧全局模型名称不再作为有效字段出现。
- 补齐文档已声明、但云端未注入的 FOLLOW_BUILDERS_ENABLED/FEED_URL、TAVILY_SOCIAL_MAX_RESULTS、PARADIGM_ALLOW_UPDATES、LOG_LEVEL；现有默认行为不变，未新增对应 Repository Variables。
- 配置状态页优先显示进程环境；即使没有 .env 也能检查。缺失角色明确显示未配置，不把向导示例当运行时 fallback；密钥完全遮蔽，不输出首尾片段。
- 清理不可达旧全局 Key 的脱敏列表项，保留两套新角色 Key 在日志、审计、邮件及持久化错误中的脱敏覆盖。
- 明确 Variables 映射、Workflow inputs、本地 SCHEDULE_*、固定云端邮件策略与高级参数的不同作用面。不把不同预算/窗口/搜索域名当作重复项合并。

本轮 Python 3.11.15 完整离线门 **848 项通过，61.802 秒**，含 5 项配置表面/状态/密钥遮蔽/文档链接回归；标准库通知、无请求导入与 compileall 阶段通过。30 项离线静态配置检查无阻断，3 份 Workflow YAML 解析与 git diff --check 通过。所有测试清除生产配置、忽略真实 .env、拒绝网络，邮件使用模拟。

后续协议/验收修复：角色 REQUEST_OPTIONS 控制输出参数/扩展/省略项，不靠模型和域名白名单；MAIN/SUB 不同模型、服务和价格共享总费用保护；核验日期来自数据不是代码日历；OpenAlex 身份查询按样本显式限 0–8 次，SMTP 和其他收费平台仍隔离。新增模型/地址迁移、特殊协议配置、日期换年、坏报价、异价共享预算和并发身份上限回归；该版完整离线门 859 项通过（58.853 秒），对应 Runner 结果见下段。

本轮已执行：[模型冒烟 38545074410](https://github.com/Claude721/AI-Paradigm-Tracking/actions/runs/38545074410) 两角色 JSON/用量通过，2 请求/81 tokens，原价估算 0.0005751 元；[3 天单材料 38545778689](https://github.com/Claude721/AI-Paradigm-Tracking/actions/runs/38545778689) 2 请求/26297 tokens，估算 0.3081041 元，observe 判断闭合、6 次 OpenAlex 查询、2 人保留无身份匹配边界。均执行 84b3dd5、Runner 859 项离线门通过，无邮件/生产状态/Tavily。首轮模型正文仍有无依据模块命名，不能把业务闭合当作事实质量已通过。

据此补修综合的原文前缀截断及路线作者缺正文：保持一手正文/尾部条件进入下游，明确初筛结果不是事实来源、执行代理与被部署模型不能混为一体；不增加新研究层或降低准入。用量细分未报告记 null，不推断零思考/零缓存。最终本地完整离线门 **862 项通过，57.904 秒**，Runner 同版本 862 项通过。[复核 38560938780](https://github.com/Claude721/AI-Paradigm-Tracking/actions/runs/38560938780) 执行 d27e1ca，同材料 observe 闭合，未再出现上述模块命名；2 请求/28029 tokens、估算 0.3386499 元。复核未重复人物查询，未产生正式稿。三轮模型总估算 0.6473291 元，完整证据及边界见[本轮验收](MODEL_ACCEPTANCE_20261014.md)。

## 历史真实证据，不冒充新模型认证

- [Run 38418943043 的 Smoke](RUN_38418943043_SMOKE_REVIEW.md)：当时 Qwen 3.7 Plus 最小 JSON 成功、7 类原点能力样本可用；arXiv 429 与官网 403 保留，Tavily/额外平台/SMTP 跳过。它不证明当前两套模型配置。
- [两次 3 天单材料研究](RUN_38500208389_RESEARCH_SAMPLE_REVIEW.md)：共 5 次、54,177 tokens，保守费用 0.532764 元；执行成功，但自动草稿合并不同实验条件、人物例外不合格，人工否决交付。后续修复未追加真实质量复验，不将离线通过写成语义正确。
- [来源 Runner](RUN_38347140838_SOURCE_REVIEW.md)、[NYU/Tavily 子集](RUN_38370843944_INTERFACE_REVIEW.md)、[历史状态迁移](STATE_V9_REPLAY_20261012.md)：分别只证明当时各自协议、授权范围与迁移守恒。Tavily usage 200 不证明搜索已执行；公开官网失败不证明 API Key 失效。

原始 RUN_*、SOURCE_REPAIR_*、STATE_V9_* 记录保留版本、费用和授权边界，不改写为当前使用指南。无需让全部物理入口全绿或历史队列清零，才能研究健康材料。

## 尚未完成的验收

1. 当前 MAIN/SUB 的真实连接、JSON、完整响应与基本用量已通过；未来更换配置仍先做有界 Smoke，不需添加代码白名单。
2. 非空中文 memo 人工核验；当前同材料事实纠偏已复核，但候选仍为观察，没有经过正式写作。首轮身份查询已执行但无法与当前博客可靠对齐，不把同名合并或关闭查询写成成功身份。
3. 正式有界计划的耗时与真实用量、总编质量、SMTP 确认、V0 状态保存、同窗恢复/去重与跨周推进。

完整正式运行的时间预算不是人民币硬限额，额外平台与自动预算续跑不能自动开启。本轮真实调用仅限已授权的冒烟/同材料单路线、20 元累计内；未发信、未改生产状态或 Secrets/Variables。所有缺口与历史失败保持真实，不把模型接口/研究判断成功改写成整周交付认证。

## 文档同步

按 neat-freak 对根文档、全部 docs 与实际实现进行核对：README 已改为流程、双层模型、最小配置、常用参数、工作流模式及失败恢复指南；配置清单/云端/设计/实施计划/单路线说明同步当前契约。AGENTS.md 链接到 CLAUDE.md，只补独立模型与云端映射两条稳定规则，未新增历史叙事或全局记忆。历史记录无需改动、未删除。

入口：[README](../README.md)、[设计](PARADIGM_RADAR_DESIGN.md)、[配置](CONFIGURATION_CHECKLIST.md)、[云端运维](CLOUD_AUTOMATION.md)、[信源说明](../信源说明.md)。
