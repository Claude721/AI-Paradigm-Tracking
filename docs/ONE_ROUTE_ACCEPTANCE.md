# 有界单路线真实研究验收

2026-10-12 授权：最多一条路线，单次人民币 API 总费用不超过 20 元，不发邮件、不自动续跑。独立工作流 `one-route-acceptance.yml` 仅有手动触发，默认不授权；不使用正式工作流的 seed 伪装缩小研究范围，不修改历史任务清单。输入为单个普通论文 arXiv ID、费用上限、官方价格核验日期与 `live_authorized=true`；不接受多条材料或多机制 Technical Report。

保留当前正式模型：**北京百炼 qwen3.7-plus**。2026-10-12 核验 [官方价格](https://help.aliyun.com/zh/model-studio/model-pricing)、[模型容量](https://help.aliyun.com/zh/model-studio/qwen3-7-plus) 和 [兼容 API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)：最高阶梯每百万输入 6 元、输出 24 元；不计免费额度、缓存或限时折扣。只有核验当日可运行；其他模型/地域、未来日期必须重新核验，不能套用代理报价或静默更换为 DeepSeek。

请求前按整个 100 万 token 输入容量和全部输出上界预留、先落盘，不用字符/tokenizer 估计作保证。`max_completion_tokens` 限制思考与回答之和，预留包含最多 10 token 的官方误差；每次最多 8000 输出 tokens，整个样本最多 24 次模型请求，结构修复也重新过费用门。SDK 隐式重试关闭。响应模型和完整用量契约正确才按最高阶梯结算；超时、取消、未知/矛盾用量、超界或落盘失败不退款，阻断后续请求。已有费用账本拒绝重建，不自动重跑或提高限额。

费用保证依赖供应商遵守已核验价格、容量与用量协议；账本不是提供方账户硬限额，不限制其他客户端，也不是最终账单。它仅保护本次验收，不替代生产月度费用设计。真实模型响应不等于人工编辑质量已验收。

验收现固定 `LLM_ENABLE_THINKING=false`，通过生产共享客户端显式发送；仍以 `max_completion_tokens` 保护总输出，不回退成只限制可见回答的 `max_tokens`。首轮 run 38235929382 默认思考耗尽 5000 总输出上限，JSON 两次截断，最高阶梯保守费用合计 0.322668 元，用量全部确认。修复后只复测同一 Titans 普通论文，下一次预算 19 元，连同首轮保守已知费用仍小于用户本次授权 20 元。没有增加论文、自动续跑或 SMTP。见 [首轮模型证据](RUN_38235929382_MODEL_REVIEW.md)。

复测 [run 38238493094](RUN_38238493094_MODEL_REVIEW.md) 已真实完成：两阶段 JSON/Rubric 闭合，0 思考 tokens、stop，保守费用 0.230898 元，两轮合计 0.553566 元。只得到 observe 路线判断，身份、承接、人物模型和非空写作并未全部验收；不能为绿色结果绕过准入。后续版本新增 `verification_summary`，展示发布者、实质讨论、覆盖/查询状态与联系方式检索数量，不存临时正文；模型输出截断在解析前阻断，即使前缀语法合法也不取得完成标记。

精确读取 arXiv 输入并核验身份/基数、一手正文后才支付模型。使用正式抽取、Rubric、增强、综合、人物分析、深挖检查点和路线写作闸门；只用临时 SQLite，绝不恢复/写入生产状态。保留 GitHub 公共只读凭据；移除 SMTP、OpenAlex、Tavily、X、Reddit、Semantic Scholar 等其他凭据并关闭其增强，平台盲区如实记录。这一范围不同于生产，不假称覆盖完整。

`screening_closed` 只证明筛选有结论，`route_judgment_closed` 只证明观察/淘汰判断已完成；都不能声称非空报告写作通过。只有实际准入、人物/原文输入与逐路线质量闸门通过才记 `route_quality_passed`。不调用整周报告 `generate()`、outbox、邮件或续跑，不生成成功空周报或阶段性邮件。历史样本只测接口/机制链路，不当本周新发布、不成为固定每周 seed 或日期依赖发布门。

制品 `paradigm-radar-one-route-<run_id>` 含费用、脱敏审计和测试结果，不含 Key、prompt、私有推理或临时数据库。通过准入时可附路线测试正文，但不是正式周报。仍须人工核对判断质量；一次样本不替代全部信源、整批闭合、同窗恢复、去重和真实交付验收。
