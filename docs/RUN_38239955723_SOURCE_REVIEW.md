# 新版 Runner 信源复验：Wayve 已修复，九项仍阻断

> 历史诊断记录：保留当时版本、授权与观测，不定义当前 V0 验收规则。文中的“全部物理入口/历史积压必须闭合”“不得重置状态”等要求已由 2026-10-13 用户澄清取代；当前按[有界研究设计](PARADIGM_RADAR_DESIGN.md)交付自身已核验的路线，故障仍保留审计。

2026-10-12，用户授权的 [run 38239955723](https://github.com/Claude721/AI-Paradigm-Tracking/actions/runs/38239955723) 执行 `9a05890c2c10bb72bbe1d7ce6ff951541becfc92`，参数 `source_check_only=true`、`smoke_only=false`、`reset_state=false`、`source_platform_checks=false`，lookback 7，无 seed/指定续跑批次。不调用模型、全部邮件、生产库恢复/上传或续跑。

Runner Python 3.12.15 的 693 项离线门通过（97.675 秒）。源码指纹 `2c66a3a061fffa19ae6869ea93507f50d91dffdbe1764feee86948ee3d894078`，执行中未变。输入清单哈希 `8cc40433dc2327fa65d7090d3d189021255d0b09f2b3f5f986b225458d1bb3fc` 与 [首轮 38232622479](RUN_38232622479_SOURCE_REVIEW.md) 相同，未删除失败入口或缩小分母。

160/160 项返回，**147 passed、9 failed、1 not_tested、3 disabled**；205 请求、186.302 秒。工作流因验收不完整正确失败，总 job 5 分钟。arXiv/OpenReview/OpenAlex/GitHub 有界协议样本通过；没有本次 Key 鉴权错误证据。不将一次样本通过解释为全分页或长期稳定。

Wayve：目录仍为 17 条详情，本次只选 1 条；3 GET、2.895 秒，29,331,554 bytes 的 GAIA-2 官方 PDF 全量读取，实际生产解析与临时 SQLite 回读 1/1，0 detail_failures，**16 pending**。这关闭下载大小导致的入口样本故障，不证明 17 条全部完成。普通响应仍限 10 MB、真实 PDF 签名响应 40 MB，不降低正文/日期/身份门槛。

| 未通过入口 | 真实观测 | 归因/未闭合边界 |
|---|---|---|
| OpenAI research index | 403，challenge | 提供方访问挑战；非 API Key |
| Meta publications | 200，仅 45 bytes | 未得到研究目录；非成功零发布 |
| PI research | 429，challenge | 提供方访问挑战；不重打绕过 |
| TRI publications | 403 | 公开访问拒绝 |
| xAI news | 403 | 公开访问拒绝，与 X Bearer 配置无关 |
| Z.ai blog | 404 | 目录迁移/地址需核验，不能用已知文章冒充完整目录 |
| NYU bibliography | 200，书目未闭合 | 身份补全默认关闭；原文/日期/详情仍有待办 |
| Gary Marcus Feed | 403，challenge | 提供方访问挑战 |
| Zvi Feed | 403，challenge | 提供方访问挑战 |

NYU 2,108 条目录书目中 146 条是窗口**年份**候选，不是 146 条本周新发布；本次保留 133 条详情 pending、1 条详情失败、139 条未闭合引用，解析任务的 reason 为 `resolution_disabled`。年份不是精确发布日期；不能清掉待办或假定其全是近期论文。配置中已有显式有界身份补全能力，但本次没有获得额外搜索授权或擅自开启。

Tavily 仍 `not_tested`，需独立平台验收授权；X/Reddit/Semantic Scholar 为声明关闭，不计 passed。不能将不覆盖平台写成零讨论。`acceptance_complete=false`、`campaign_coverage_complete=false`：下一次直接全量研究仍会因这些缺口失败，增加模型调用不解决公开 HTTP 访问/书目身份问题。

制品 ZIP 7 文件、112,545 bytes，SHA256 `fd82758cd1bdb957c0c2b4495a4b1594f3a5d81877df31add9d5e736b2697f35` 与 Runner 摘要一致。原始 ZIP/日志/库/报告不提交。

随后另修复模型 Smoke 只按同名模型去重的问题：仅 provider/model/base_url/凭据完全相同才共用验收，凭据不输出。最终 Python 3.11/3.12 各 695 项门通过；这项修复不改变来源清单或来源适配器，但晚于本 run，不能将本 run 称为最终 695 项云端验证。模型、实库与交付边界见 [模型复测](RUN_38238493094_MODEL_REVIEW.md)、[迁移证据](STATE_V9_REPLAY_20261012.md)。当前不是生产 V0。
