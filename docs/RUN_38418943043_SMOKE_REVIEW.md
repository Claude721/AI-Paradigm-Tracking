# V0 小范围真实冒烟验收

2026-10-13（上海时间 23:04–23:07）。[GitHub Run 38418943043](https://github.com/Claude721/AI-Paradigm-Tracking/actions/runs/38418943043) 已完成，结论 success。

## 执行范围与版本

- 验证代码 867d2d24609debb15cd78960992a85726f60d8e2；Runner provenance 与 workflow SHA 相同，工作区干净，Python 3.12.15。
- 源码指纹 3c749ace42da117cdbc1006038f4cf30644549b0d1e65f363d2d33aa42c3f9c2。
- 一次 workflow_dispatch：smoke_only=true、source_check_only=false、source_platform_checks=false、reset_state=false，seed 与 resume_campaign_id 为空。
- lookback_days=3 仅是本次 dispatch 参数；Smoke 使用连通性样本，没有执行 3 天窗口的正式研究。
- 模型每个独立配置最多一次请求，输出上限 64 tokens，完整 JSON 契约验证，不自动重试；两个角色共用模型/地址/Key，因此只调用一次。
- Tavily（包括 /usage）、X、Reddit、Semantic Scholar 和 SMTP 均跳过。研究、状态恢复/保存、报告保存、续跑和故障邮件步骤均 skipped；唯一制品是审计 ZIP。
- 未修改 Secrets/Variables，未删除旧数据库或制品；云端已有 PARADIGM_RECALL_OVERLAP_DAYS=30 仍覆盖生产默认 14，但不参与本次 Smoke。

## 实际结果

| 能力 | 结果 | 证据与边界 |
|---|---|---|
| Qwen JSON | passed | dashscope/qwen3.7-plus；1897 ms；完整 JSON；输入 29 / 输出 5 / 合计 34 tokens |
| arXiv | degraded | 单次精确 ID 请求 HTTP 429；保留 transient_availability，不继续分页或把失败写成成功 |
| Hugging Face Papers | passed | 50 个样本；只证明接口及响应结构，不证明本期时效或范式资格 |
| OpenAlex Works / Authors | passed | 各 2 个样本，鉴权及响应结构正常；作者查询是身份接口探针 |
| OpenReview | passed | ICLR.cc/2026/Conference 查询返回 1 个 notes 样本 |
| 官方页探针 | passed with prior failure | openai.com HTTP 403 记入尝试账本，www.anthropic.com 索引 HTML 成功；不等同于所有官网/详情生产解析验收 |
| Google 研究 Feed / 高信号论坛 / KOL Feed | passed | XML/条目契约可用；不拿累计条目数当本周新增，不推断精确 karma |
| Follow Builders / GitHub Search / HN | passed | 只证明补充传播检索连通性；awesome list、历史 HN 热帖不能直接形成技术路线 |
| 输入能力汇总 | passed | 7 类原点召回能力样本可用，默认最低要求 2 类；每条路线仍须自己的来源/质量门 |

Smoke JSON：13 passed（含模型与汇总项）、1 degraded、0 failed、6 skipped。13 不是 13 个入口全部验收，更不是 13 条合格路线。边界字段 production_v0_accepted=false。

原始审计制品 paradigm-radar-audit-38418943043（ID 11825297266，35,538 bytes）包含 smoke_test_latest.json、runtime_provenance.json、offline_checks.json / .log、doctor.log、dependencies.log、current_run.log；未将这些生成内容提交到仓库。

## 用量与费用

API 确认 29 输入、5 输出、34 合计 tokens；不是把缺失 usage 算成 0。按[阿里云官方 qwen3.7-plus 北京价格](https://help.aliyun.com/zh/model-studio/qwen3-7-plus)，小于 256K 输入档为输入 2 元 / 百万 tokens、输出 8 元 / 百万 tokens，估算 0.000098 元（不使用缓存/免费额度折扣）。这是按公开价格估算，不是供应商账单确认，远低于此次 20 元授权上限。

## 回归与结论

- 本地 Python 3.11.15：813 项通过，58.383 秒；compileall、3 份 workflow YAML 解析、git diff --check 通过。
- Runner Python 3.12.15：813 项通过，117.634 秒；其余离线发布门阶段通过。
- 真实接口冒烟步骤约 7 秒；整轮约 2 分 27 秒，主要时间用于离线发布门。
- 这次证明模型最小 JSON 调用、公开/配置输入基础能力及单入口故障隔离可用；arXiv 429 与公开 HTML 403 是访问层缺口，不据此要求重配全部 API Key，也不扩大重试压力。
- 未验证真实机制抽取、外部承接判断、人物归因、中文报告质量、邮件、同窗/跨周生产状态，所以不能据此宣布完整稳定 V0。

下一层应是独立状态、不发邮件、不自动续跑、保持 20 元边界的 3 天真实研究小样本；不能直接关闭 Smoke 后把正式周任务当成低费测试。先对真实样本完成的证据与中文报告进行验收，不再以所有官网全绿为前置条件。
