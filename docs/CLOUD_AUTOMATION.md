# GitHub 云端运行与 V0 验收

## 默认工作流

weekly-radar.yml 每周五 09:15（Asia/Shanghai）运行，Ubuntu / Python 3.12，90 分钟上限。研究 3600 秒、报告 1200 秒，留出依赖、离线门、邮件和制品时间。本机不用开机。

定时触发会调用已配置模型及邮件；人工测试仍需用户授权。代码修复和上传本身不是新增付费测试/发信授权。

## 手动选项

| Input | 作用 |
|---|---|
| smoke_only=true | 模型最小 JSON、公开/OpenAlex/GitHub 输入能力；不调用 Tavily/X/Reddit/Semantic Scholar、SMTP，不发信或恢复生产状态 |
| source_check_only=true | 优先于 Smoke；逐入口 HTTP/生产解析/临时 SQLite 检查，无模型/SMTP/生产状态 |
| source_platform_checks=false | 默认不测付费/需批准平台；显式开启才做已启用平台的最小有界请求 |
| reset_state=false | 使用 V0 状态；首次 V0 无快照则自动建立干净 epoch |
| lookback_days | 正式普通窗口支持 3/7/30/60/90 天；3 天仍是会调用研究模型及邮件的正式运行，不是低费用保证 |
| seed_arxiv_ids | 临时材料可见性，不提高技术准入 |
| resume_campaign_id | 严格恢复指定原计划，必须关闭 Smoke 且不重置 |

正式运行需 smoke_only=false、source_check_only=false。不必等待所有官网检查全绿。

云端 smoke_only=true：每个独立模型配置最多一次请求、输出上限 64 tokens、验证完整 JSON；相同模型/地址/Key 去重，SDK 不自动重试。原点召回能力门槛沿用 PARADIGM_MIN_PRIMARY_SOURCES（默认 2 类），其他入口失败留诊断，不要求全绿；模型鉴权、JSON 契约或有效输入不足仍失败。JSON 制品保留实际输入/输出/总 tokens，缺用量标 unknown，不能算费用为零。Smoke 不是正式判断或写作质量验收；不会自动串联 3 天研究。

## 模型、信源与邮件配置

Settings → Secrets and variables → Actions：

- 模型 Repository Variables 必填：LLM_PROVIDER（当前 dashscope）、LLM_MODEL（当前 qwen3.7-plus）、LLM_BASE_URL（当前 https://dashscope.aliyuncs.com/compatible-mode/v1）。正式/Smoke 与单路线工作流直接读取，无硬编码兜底；缺失时调用模型前明确报配置错误。纯信源检查不要求模型配置。
- 通用 Secret：LLM_API_KEY，留空兼容 DASHSCOPE_API_KEY；两者同时存在时前者优先。API Key 不放 Variables。
- 角色覆盖：SUB_AGENT_PROVIDER/MODEL/BASE_URL 与 MAIN_AGENT_PROVIDER/MODEL/BASE_URL（均使用完整角色前缀）；对应 SUB_AGENT_API_KEY、MAIN_AGENT_API_KEY Secrets 可选。
- 不覆盖角色就把角色 Variables 和角色 API Key Secrets 全部留空继承全局。换提供方修改三个全局 Variables 与对应 Secret，无需改代码；同时核对模型、地址、Key，不限 Qwen。地址填兼容 API 根地址，不是 /chat/completions 完整路径。
- 推荐 OPENALEX_API_KEY；GITHUB_TOKEN 自动取 github.token，不另建同名 Secret。
- SMTP_USERNAME、SMTP_PASSWORD、SMTP_TO Secrets；现有 QQ SSL 发送路径不变。
- X、未批准 Reddit、未批准 Semantic Scholar 保持关闭，不为 V0 新增付费平台。
- 参数与范围详见 [配置清单](CONFIGURATION_CHECKLIST.md)。已有 30/60 天回补 Variables 改为 14 或删除使用默认；自动预算续跑保持 false。

## V0 状态 epoch

当前 schema v10 增加 superseded 研究计划与范围交付凭据，保留此前检查点、outbox 和 credit 数据结构。

云端只恢复 paradigm-radar-v0-state-<run_id>。第一次没有这种快照时从空库启动，旧 paradigm-radar-state-* 未删除但不再导入旧失败队列。用户已允许丢弃无效历史，这里用独立 namespace 实现，不直接删除旧制品。

每次运行后以不可变 run_id 快照保存。下载副本先执行 quick_check、schema/领域对象/任务/额度/链接契约，再覆盖工作库；较新快照损坏可回退较早健康 V0 快照。已有 V0 快照全部不可用则失败，不静默冷启动。reset_state=true 是显式重建，不是普通报错解决方法。

恢复合规 outbox 优先完成报告/邮件，不回滚研究、不吞旧投递；成功续投先上传状态，再单独排队新研究，不能在同一 90 分钟进程叠加。SMTP 接受后进程硬退出仍可能按同 Message-ID 至少一次重试。

## 研究与交付

默认有界计划：48 原点、8 深挖、4 历史更新；7 天普通/14 天高信号，保留跨源广度。单官网/Feed/API 故障只记录缺口，健康材料继续。不同发现通道不是独立复现。

非空报告只包含自身技术、时效、人物和原文核验完成的路线。无路线时，本期选定研究全部完成且有效取材才可空报告；核心判断失败不能说“没有重要技术”。不发送阶段性 memo 或待续研清单；邮件统计描述已完成初筛/深挖，不把未选材料或故障对象宣称全部完成，主题使用“新路线”而非直接断言范式已经形成。

研究诊断可以仍有 research_incomplete/coverage_incomplete；v2 正式交付由 delivery_scope 与真实 SQLite 核对。pipeline_result 的 fresh_research_completed 依据交付完成契约，不再要求全局诊断无缺口。范围完成不表示全网覆盖或成熟范式认证。

报告的每条路线及全文每个 URL 必须可追溯；人物不得同名误配或猜邮箱，正文中文、无评分表。编辑质量失败明确失败。EMAIL_PUSH_REQUIRED=true 后 SMTP 失败也明确失败，不能提前登记 last_reported_signature。

## 信源诊断与独立测试

source_check_only 采用 --v0-readiness：有至少 2 类可用一手输入样本时可通过输入能力检查，但每个 failed/not_executed/not_tested 仍保留，acceptance_complete 不会被改成 true。这不是正式研究或生产 V0 认证。

不带 --v0-readiness 的本地 scripts/source_audit.py 仍是严格全清单诊断；有缺口就非零退出。--entry/--kind 子集不能声明 V0 输入能力完成。诊断不恢复生产库，不调用模型或邮件，不发送失败提醒。

source_platform_checks 只有单次明确授权后才开启；Tavily/X 可能消耗额度，不自动加钱或绕过挑战。401 检查 Key，403 区分权限/网络护栏，429 区分频率/额度，404/解析空目录检查路径/格式，不整体轮换所有 Secrets。见 [来源诊断](SOURCE_ACCEPTANCE.md)、[Key 指南](SOURCE_API_KEY_VERIFICATION.md)。

独立 source-interface-acceptance.yml 仅验授权 NYU/Tavily，默认 live_authorized=false，无模型/邮件/生产状态。NYU 最多 8 次身份 GET，Tavily 在包含额度/PAYG 条件明确后最多 1 次 basic；未知限额不搜索。2026-10-13 单次授权已经执行，后续用户要求暂不调用，不自动追加复验。

one-route-acceptance.yml 是窗口内单路线模型测试，默认 3 天，最多 20 元、无邮件/生产状态。arxiv_id 与 primary_url 必须且只能选一项；后者必须在已登记官方研究索引中原样出现，并由生产解析器确认正文与原始日期。旧论文、仅修改日期、无原始日期不支付模型；拒绝多机制 Technical Report，不为样本截断或放宽准入。费用保护本版绑定已于 2026-10-14 核验的北京 Qwen 当日价格；模型/地址沿用全局配置，其他模型明确停止而非静默套价。研究采用 V0 部分来源容错，但本条路线自身仍须完成真实判断。

历史 Runner 证据见 [来源](RUN_38347140838_SOURCE_REVIEW.md)、[接口](RUN_38370843944_INTERFACE_REVIEW.md)。当时全入口闭合目标不再适用 V0 交付。

## 制品、权限与故障

- paradigm-radar-v0-state-<run_id>：数据库及 state-meta.json，90 天保留。
- paradigm-radar-audit-<run_id>：去敏审计、current_run.log、离线门、Doctor、状态恢复/准备诊断。
- 本期报告制品：合规 Markdown，报告/邮件失败不丢研究检查点。
- 独立接口/单路线制品使用各自 namespace，不污染生产状态。

contents: read；actions: write 只用于已有 outbox 恢复后的新研究或显式授权预算续跑，不推送仓库。checkout 不持久化凭据、不下载无关子模块。各步骤有稳定 id，故障通知区分依赖、配置、状态、研究、SMTP 和上传失败；平台硬取消/Runner 故障不保证后置步骤执行。

离线测试清除生产 Variables/Secrets、忽略 .env，代理拒绝网络；还隔离 GITHUB_STEP_SUMMARY/OUTPUT/ENV/STATE/PATH，防止 fixture 污染真实 Runner 结果。

PARADIGM_AUTO_RESUME_ENABLED=false 默认不扩大支出。开启后只在状态上传成功、同批纯预算延期、次数/时间/已确认 tokens 未达限时排队；覆盖/执行故障与未知用量停止自动链。MAX_TOKENS 是轮间停止线，不是人民币单请求硬费用上限。

## 生产验收

部署代码后，先确认执行 SHA 与有效配置，再进行授权正式运行。从真实审计看本期选定/完成数、合格路线、用量与质量，而不是只看 Workflow 绿色。确认邮件、V0 状态上传、重复投递跳过、下一周新窗口推进。

离线通过是工程发布条件，不是真实模型质量或生产稳定性证明。最近真实测试及尚未验收边界见 [工程状态](PROJECT_HEALTH_REPORT.md)。
