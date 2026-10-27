# 有界单路线真实研究验收

## 当前入口与配置驱动契约

本指南描述最近 3 天、最多一条路线、独立状态、无邮件/自动续跑的小样本验收；默认费用上限 20 元。实际付费调用须当次明确授权，本文和历史记录本身不构成持续调用许可。输入 arxiv_id 或 primary_url 二选一；后者只接受内置官方研究目录中真实链接的单篇文章。必须用原始发布时间落在运行冻结的 72 小时内，并读取一手正文，才调用模型。不会将 HF 收录时间、网页修改时间或旧论文 seed 视为近期发布。HF 原点日期与平台收录日期已分栏保存。

以下价格仅为 2026-10-14 核验示例，不代表当前模型或有效报价：[Qwen 3.8 Flash](https://help.aliyun.com/zh/model-studio/qwen3-8-flash)、[Qwen 3.8 Max](https://help.aliyun.com/zh/model-studio/qwen3-8-max) 及[兼容 API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)。当时北京原价分别为每百万输入/输出 0.8/2.7 元与 12/36 元；完整输入上限 991808，使用总输出限制与 10 token 误差。运行配置从 MAIN/SUB Variables/Secrets 读取，不写入价格代码；每次验收须重新核对实际接口与价格。

工作流输入 `price_contracts` 为 `{ "sub": {...}, "main": {...} }`。每个对象须且只含以下字段，模型、地址与核验日期必须匹配当前实际配置；其他厂商/模型使用其真实契约值，不套用示例价格。`pricing_verified_on` 为运行当日的 UTC 日期 YYYY-MM-DD，不存在代码日历白名单：

```json
{
  "model": "qwen3.8-flash",
  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "input_limit": 991808,
  "input_price_cny": "0.8",
  "output_price_cny": "2.7",
  "output_limit": "max_completion_tokens",
  "output_tolerance": 10,
  "response_models": ["qwen3.8-flash"],
  "verified_on": "填写核验当日 YYYY-MM-DD",
  "pricing_url": "https://help.aliyun.com/zh/model-studio/qwen3-8-flash",
  "contract_url": "https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions",
  "completion_includes_reasoning": true
}
```

上面是 sub 对象的模板，不是可直接执行的整份输入。使用 workspace 地址时填实际 API 根地址；main 对象填当前 MAIN 模型与官方原价、容量、响应别名。先核验提供方确实按该参数限制思考与回答合计，才能设置 completion_includes_reasoning=true。不存在已核验价格时停止，不能改为无预算调用；正常生产并不要求提供样本价格。

初筛读取 SUB，深挖/人物/写作读取 MAIN。两个实际客户端分别核验，共享一个费用账本/锁/20 元停止线，不是每层各 20 元。每次预留完整输入容量，不依赖 tokenizer、免费额度或缓存折扣；响应有效用量才结算，未知扣费阻断所有角色。`models_only=true` 只各验一次模型，两个原点输入须留空。正常单路线 `identity_requests=0–8` 可保留已有 OPENALEX_API_KEY 做有界人物核验，Tavily/X/Reddit/Semantic Scholar/SMTP 仍隔离，盲区如实记录。

研究与增强沿用 V0 部分来源容错，Rubric、一手 URL 和中文路线质量门不放宽；人物归因是软约束，未核验事实不得写入。observe/reject 形成明确的样本判断，不改写为成功空周报；完成判断后才生成 sample_review.md，执行失败只留诊断，绝不生成阶段性 memo。JSON/Markdown 制品明确 production_v0_accepted=false；不调用整周总编，不宣称覆盖所有 3 天信息。

真实测试的自动 route_quality_passed 不代替人工事实核验。2026-10-14 同材料复测曾发现未核验归因和跨模型/环境合并实验的问题，历史结论保留。当前人物未知本身不否决技术，虚构身份、隐瞒实验限制或合并不同实验条件仍不可接受。最新证据见[项目工程状态](PROJECT_HEALTH_REPORT.md)。

## 历史验收与费用证据

> 以下是旧版 Qwen 3.7 费用与隔离契约的历史记录，不是当前模型/价格/身份接口限制。当前使用上文配置驱动契约；新付费验收不能沿用其他提供方或过期价格，也不能据此自动执行。

2026-10-12 授权：最多一条路线，单次人民币 API 总费用不超过 20 元，不发邮件、不自动续跑。独立工作流 `one-route-acceptance.yml` 仅有手动触发，默认不授权；不使用正式工作流的 seed 伪装缩小研究范围，不修改历史任务清单。输入为单个普通论文 arXiv ID、费用上限、官方价格核验日期与 `live_authorized=true`；不接受多条材料或多机制 Technical Report。

2026-10-12 验收使用的模型是 **北京百炼 qwen3.7-plus**，不是当前 Repository Variables 的固定要求。2026-10-12 核验 [官方价格](https://help.aliyun.com/zh/model-studio/model-pricing)、[模型容量](https://help.aliyun.com/zh/model-studio/qwen3-7-plus) 和 [兼容 API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)：最高阶梯每百万输入 6 元、输出 24 元；不计免费额度、缓存或限时折扣。只有核验当日可运行；其他模型/地域、未来日期必须重新核验，不能套用代理报价或静默更换为 DeepSeek。

请求前按整个 100 万 token 输入容量和全部输出上界预留、先落盘，不用字符/tokenizer 估计作保证。`max_completion_tokens` 限制思考与回答之和，预留包含最多 10 token 的官方误差；每次最多 8000 输出 tokens，整个样本最多 24 次模型请求，结构修复也重新过费用门。SDK 隐式重试关闭。响应模型和完整用量契约正确才按最高阶梯结算；超时、取消、未知/矛盾用量、超界或落盘失败不退款，阻断后续请求。已有费用账本拒绝重建，不自动重跑或提高限额。

费用保证依赖供应商遵守已核验价格、容量与用量协议；账本不是提供方账户硬限额，不限制其他客户端，也不是最终账单。它仅保护本次验收，不替代生产月度费用设计。真实模型响应不等于人工编辑质量已验收。

验收现固定 `LLM_ENABLE_THINKING=false`，通过生产共享客户端显式发送；仍以 `max_completion_tokens` 保护总输出，不回退成只限制可见回答的 `max_tokens`。首轮 run 38235929382 默认思考耗尽 5000 总输出上限，JSON 两次截断，最高阶梯保守费用合计 0.322668 元，用量全部确认。修复后只复测同一 Titans 普通论文，下一次预算 19 元，连同首轮保守已知费用仍小于用户本次授权 20 元。没有增加论文、自动续跑或 SMTP。见 [首轮模型证据](RUN_38235929382_MODEL_REVIEW.md)。

复测 [run 38238493094](RUN_38238493094_MODEL_REVIEW.md) 已真实完成：两阶段 JSON/Rubric 闭合，0 思考 tokens、stop，保守费用 0.230898 元，两轮合计 0.553566 元。只得到 observe 路线判断，身份、承接、人物模型和非空写作并未全部验收；不能为绿色结果绕过准入。后续版本新增 `verification_summary`，展示发布者、实质讨论、覆盖/查询状态与联系方式检索数量，不存临时正文；模型输出截断在解析前阻断，即使前缀语法合法也不取得完成标记。

精确读取 arXiv 输入并核验身份/基数、一手正文后才支付模型。使用正式抽取、Rubric、增强、综合、人物分析、深挖检查点和路线写作闸门；只用临时 SQLite，绝不恢复/写入生产状态。保留 GitHub 公共只读凭据；移除 SMTP、OpenAlex、Tavily、X、Reddit、Semantic Scholar 等其他凭据并关闭其增强，平台盲区如实记录。这一范围不同于生产，不假称覆盖完整。

`screening_closed` 只证明筛选有结论，`route_judgment_closed` 只证明观察/淘汰判断已完成；都不能声称非空报告写作通过。只有实际准入、人物/原文输入与逐路线质量闸门通过才记 `route_quality_passed`。不调用整周报告 `generate()`、outbox、邮件或续跑，不生成成功空周报或阶段性邮件。历史样本只测接口/机制链路，不当本周新发布、不成为固定每周 seed 或日期依赖发布门。

制品 `paradigm-radar-one-route-<run_id>` 含费用、脱敏审计和测试结果，不含 Key、prompt、私有推理或临时数据库。通过准入时可附路线测试正文，但不是正式周报。仍须人工核对判断质量；一次样本不替代全部信源、整批闭合、同窗恢复、去重和真实交付验收。
