# AI 技术范式雷达

从广泛的一手材料发现机制假说，再查证技术、发布者、外部承接与关键人物，最终输出中文研究 memo。不是论文日报，也不为了周更强行造出范式。

默认采用 V0 有界研究契约：尽可能拓宽取材，单个网站/API 故障局部隔离，不要求所有物理入口成功或历史队列清零。工程改动和离线验证不等于生产验收；见 [工程体检](docs/PROJECT_HEALTH_REPORT.md)。

## 核心流程

多来源取材 → 去重与来源校验 → 有界本期研究 → 机制初筛 → 外部承接/人物核验 → 路线综合 → 中文报告 → 邮件。

- 一手材料：arXiv、OpenAlex、OpenReview、官方研究博客/目录、原创思想文章、已核验官方原生实现。普通论文通常一个机制假说，正式 Technical Report 可以拆出多个独立机制。
- 补充证据：GitHub、Hacker News、HF、KOL、Tavily，以及显式开启的 X/Reddit/Semantic Scholar。作者自发帖、聚合列表和搜索命中不能冒充独立承接。
- 技术去留由 [版本化 Rubric](rubrics/paradigm_rubric.json) 与发布者/承接证据决定。研究排期与取材上限不等于技术淘汰，不绕过质量闸门。
- 报告按共同问题、机制与能力边界组织路线，解释训练/运行因果链，并给出讨论势能、关键推动者及公开职业联系入口。分数只在审计中出现。

官网 HTML 通过 HTTP 客户端与解析器读取，部分站点使用已核验公开 JSON 目录。不需要 Computer Use、浏览器登录或反爬绕过。403、429、404、访问挑战分别记入审计；其他正常通道可提供相关材料，但不能宣称原网站已经成功。

## V0 交付规则

默认普通窗口 7 天，高信号回补 14 天。arXiv 每查询最多 100 条，OpenAlex 每查询最多 2 页，已支持官方目录最多 2 页，每个官方入口新增详情最多 6 条。按发布者/来源轮转选定最多 48 个研究原点、深挖最多 8 条路线、刷新最多 4 条历史路线。保留所有发现车道与失败记录，不做全网穷尽声明。

至少 2 个一手发现通道可用才具备交付基础。这个数衡量输入可用性，不等于独立验证数量。

| 结果 | 行为 |
|---|---|
| 有完成核验且通过质量门槛的路线 | 交付这些路线，其他故障留在审计，不产生阶段性 memo |
| 无合格路线，本期选定研究全部完成 | 空雷达，结论限定为已研究材料中没有符合条件的路线 |
| 有效输入不足，或选定判断未完成且无合格路线 | 明确失败，保存检查点，不把故障写成“没有重要技术” |
| 报告质量或必需 SMTP 投递失败 | 明确失败，研究不回滚，不提前登记交付 |

每条交付路线必须通过最终 Rubric、时效、人物身份/联系入口检索和一手 URL 闸门；全文每个 URL 必须原样来自证据或已核验人物档案。不得猜链接、邮箱或任职，不能输出英文摘要和字段拼装降级稿。

研究与报告 outbox 分离。报告/SMTP 失败可复用已完成研究，确认交付后才更新已报告签名。SMTP 接受与本地确认之间仍有至少一次重试边界，不承诺跨邮箱的绝对 exactly-once。

同窗续跑复用已选计划。下个普通窗口到来时，未交付研究计划归档为 superseded，旧待办不标记成功，也不锁住新一周。待投递合规 outbox 仍优先恢复。自动预算续跑默认关闭。

## 配置与运行

Python 3.11+：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

模型不限定 Qwen。设置 LLM_PROVIDER、LLM_MODEL、LLM_BASE_URL 与 LLM_API_KEY，使用支持 Chat Completions 和完整 JSON 输出的兼容 API。角色专属 SUB_AGENT_* / MAIN_AGENT_* 默认留空继承全局；换提供方时同时核对地址和 Key。默认 Qwen 仅兼容已有部署。

OpenAlex Key 推荐配置以增强召回与人物身份，但未配置 OpenAlex/OpenReview 不是静态全局阻断。X 默认关闭，无需为 V0 申请付费 Token。Tavily 是可选索引线索，月度 credit 保护与提供方账户限额继续保留。

```bash
python main.py --doctor          # 静态检查，不联网
python main.py --status          # 查看有效模型配置
python main.py --inspect-state   # 查看队列/outbox，可执行兼容迁移
python scripts/offline_checks.py # 无网络发布门
python main.py --smoke-test      # 获得授权后：最小探针，SMTP 只登录
python main.py                   # 获得授权后：正式研究，启用邮件时会发信
python main.py --resume-research # 只恢复原活动计划
python main.py --report          # 续投合规 outbox 或重建最近合规报告
```

逐入口诊断 scripts/source_audit.py 忽略 .env，无研究模型/邮件/生产库。全清单诊断红色表示有未通过项，不要求正式研究等待每个官网修好。Smoke 不证明研究质量或生产稳定性。

## GitHub 定时运行

weekly-radar.yml 每周五 09:15（Asia/Shanghai）运行，job 上限 90 分钟。默认 3600 秒研究与 1200 秒报告，为状态、邮件和审计上传留余量。

云端 V0 使用 paradigm-radar-v0-state-<run_id> 独立状态：第一次从空库启动，不导入旧失败批次；旧 paradigm-radar-state-* 制品未删除。新 V0 状态逐次保存、恢复；已有 V0 快照均损坏时不能静默重置。手动 reset_state=true 仍可重建。

Actions 模型 Variables 与通用 Key Secret 已支持切换；现有 DASHSCOPE_API_KEY 仍是全局 Key 兼容回退。不要提交 .env、数据库、日志或生成报告。见 [配置清单](docs/CONFIGURATION_CHECKLIST.md)、[云端运行](docs/CLOUD_AUTOMATION.md)。本机无需开机，不要重复调度。

## 代码与文档

- agents/paradigm_orchestrator.py：本期计划与执行隔离。
- paradigms/discovery.py / sources/：访问与解析。
- paradigms/delivery_policy.py / completion.py：有界交付与旧凭据兼容。
- paradigms/analyzer.py / rubric.py / scoring.py：机制、技术与承接准入。
- database/research_campaign.py / paradigm_store.py：检查点、范围回执及 outbox。
- reports/paradigm_generator.py / notifications/email_notifier.py：中文写作与交付。
- [设计说明](docs/PARADIGM_RADAR_DESIGN.md)、[信源说明](信源说明.md)、[研究心智模型](docs/MENTAL_MODEL_WRITING_METHOD.md)、[来源诊断](docs/SOURCE_ACCEPTANCE.md)。

旧项目型 PIPELINE_MODE=legacy 仅保留兼容，不是 V0 默认路径。
