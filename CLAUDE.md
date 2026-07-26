# AI Paradigm Tracking 项目规则

## 目标

本项目默认运行 `PIPELINE_MODE=paradigm`：从论文、官方技术博客与二次传播中识别正在形成的 AI 技术路线，并把技术推进者及其公开职业联系方式写进研究 memo。

## 不可破坏的边界

- 普通论文通常只产生一个“机制假说”，不能直接等同于一个技术范式；正式 Technical Report 可以拆出多个相互独立、可承接的机制。
- 通过技术硬门槛后仍需核验发布者势能与外部承接：未知团队且缺少实质二次讨论的候选只能留在观察池。
- 作者本人发布工作只能用于身份与背景核验，不能算作独立二次讨论。
- GitHub、Hacker News、Hugging Face 与 KOL 内容只能补充扩散证据，不能单独生成范式。
- 论文聚合仓库、日报、awesome list 和同名噪声不得计作实现或复现。
- 最终报告按共享 background 与能力边界组织技术路线，不按论文逐条填表。
- 候选深挖必须形成内部心智模型脚手架：真实对象、训练信号、推理/行动信息流、最小实例、反事实与未闭合接口；它只用于自然写作，不得作为固定字段清单输出。
- 分数只用于内部筛选，不得出现在最终报告中。
- 技术范式不按周出现；没有新路线时允许空报告，旧路线出现本周新增讨论时可以作为进展更新。
- 报告解释必须使用中文转述，不得输出英文摘要、英文长句或字段拼装降级稿；编辑质量失败必须中止本轮。
- 人物资料只收集公开职业信息；不得猜测邮箱或把同名研究者强行合并。
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
python -m unittest discover -s tests -v
python -m compileall -q agents database paradigms reports skills sources main.py config.py
```

测试不得依赖真实网络、真实密钥或真实邮箱。报告测试使用无网络的模拟编辑客户端。

## 文档入口

- `README.md`：运行入口与系统概览
- `docs/PARADIGM_RADAR_DESIGN.md`：当前研究与去重设计
- `docs/CONFIGURATION_CHECKLIST.md`：完整配置清单
- `docs/CLOUD_AUTOMATION.md`：GitHub Actions 与云端邮件
- `docs/PROJECT_HEALTH_REPORT.md`：本轮体检结论
- `信源说明.md`：各信源的作用和限制
