# NYU / Tavily 独立接口验收：run 38370843944

> 历史诊断记录：保留当时版本、授权与观测，不定义当前 V0 验收规则。文中的“全部物理入口/历史积压必须闭合”“不得重置状态”等要求已由 2026-10-13 用户澄清取代；当前按[有界研究设计](PARADIGM_RADAR_DESIGN.md)交付自身已核验的路线，故障仍保留审计。

日期：2026-10-13。结论：**两项接口子集未通过，生产 V0 未达到**。所有者授权的有界检查已执行一次；不自动重试、不扩大费用、不改仓库 Variables、生产数据库、研究模型或邮件。此前普通全来源结果仍以 [run 38347140838](RUN_38347140838_SOURCE_REVIEW.md) 为准，不能把两份不同 scope 的计数相加。

## 执行证据

- [GitHub run](https://github.com/Claude721/AI-Paradigm-Tracking/actions/runs/38370843944)，job `115207164206`；手动 `main`、`scope=both`、`bibliography_requests=8`、`live_authorized=true`。
- 执行提交 `4cd5c6fcdbad0c1e6a103709f51c3bd142472312`，Python 3.12.15，工作区无修改；源码指纹 `c5bd95a163e4153c142fd3c58ecb613cd4f3fb9a26b5970cceb65ef2be02c5e9`，执行前后相同。
- 云端 774 项测试通过（113.288 秒），离线发布门总计 115.758 秒；真实 Step Summary 只包含两项实际接口状态，之前模拟 Key/未知用量提示没有再混入。
- 工作流 2 分 25 秒，job 2 分 21 秒；接口检查 9.831 秒，合计 11 HTTP，均返回 200，但 2 项语义/保护门失败。
- [制品](https://github.com/Claude721/AI-Paradigm-Tracking/actions/runs/38370843944/artifacts/11799375275) 54,453 bytes，SHA256 `31870be86640f2c07ace5e45e7d7d1e4622c218e20a7046fe0662483c4cec713`，下载后与上传日志一致。3 个文件仅为去敏接口 JSON/Markdown、离线门 JSON；不含生产库、模型/SMTP 日志或正式研究报告。

独立工作流没有复制所有正式研究 Variables，因此该进程声明清单为 143 项，141 项未测试，hash `d85662fac3fa31164281a5c709cfd35239cd18b6d239d5ef819587e88e1d7e57`。这不是普通工作流 160 项生产清单的缩减或重新认证。本次完整验收、批次覆盖及生产 V0 字段均为 false；后续代码增加显式 `full_manifest_scope=current_interface_invocation` / `production_manifest_verified=false`，防止误读。

## NYU：鉴权成功，书目与原文仍未闭合

10 GET：登记目录 1 次、OpenAlex Works 身份查询恰好 8 次、arXiv 正文样本 1 次。无 401/403/429、无新付款或 Key 失效证据。目录 537,843 bytes；首个 arXiv 正文 40,831 bytes，旧论文被正确排除窗口，详情故障为 0。

138 个独立未知身份任务全部保留：

| 本次状态 | 数量 | 含义 |
|---|---:|---|
| `resolved_url` | 3 | 唯一题名、年份与引用作者对齐，并返回明确 DOI；原文日期/正文仍待核验 |
| `identity_not_found` | 4 | 本次查询未找到完整匹配，不证明材料不存在或 Key 失效 |
| `identity_search_truncated` | 1 | 结果未穷尽，不能选首项猜身份 |
| `identity_request_safety_limit` | 130 | 已到授权 8 次上限，未查询，不删除尾项 |

3 个已解析 DOI 属于 IoT 预测性维护、相关技术综述、MJEPA；只是链接发现，不能据此生成范式或报告。134 个原显式链接加 3 个解析链接得到 137 个详情身份；仅核验一个，136 个仍未读。`unresolved_citations=138` 保持不变，因为本次没有核验新链接的一手正文。它们都是目录任务，不是 138 篇本周新论文。

一个带内层引号的长题名搜索返回 872,678 bytes 并触发截断。已确认旧代码使用通用 `search`，会搜索标题、摘要与全文；[OpenAlex 官方协议](https://help.openalex.org/api/searching/) 支持 `search.title`。后续修复只搜索规范化的完整标题短语，避免引号/布尔词成为检索语法或被正文引用干扰；仍要求本地完整题名、年份、引用作者、唯一 Work ID、一手 URL 与正文日期，截断/重复/错误对象仍待办。保留原标题、缓存身份、历史清单和尝试计数，不新增真实调用；这项新查询协议目前只通过离线验证，不能冒称云端已复验或四个未匹配均已修好。

## Tavily：只读 200，付费保护阻止搜索

仅 1 GET `/usage`，200 / 312 bytes / 1.239 秒；**没有 `/search` POST**。`tavily_included_payg_limit_unknown` 表示 `account.paygo_limit` 未满足明确非负整数契约，不能确认 PAYG 关闭。它不是 401、Key 无效、额度耗尽或“已知 PAYG 开启”的证明。本制品未保存字段值或类型，不能进一步断言是缺失、null、字符串还是其他类型；不能把未知默认写成 0。

[Tavily Usage 官方协议](https://docs.tavily.com/documentation/api-reference/endpoint/usage) 提供账户和 Key 用量/限额。后续修复只增加六个固定字段的类型标签、HTTP method 去敏回执，不记录响应文本、Key、账户标识或账单；保持未知/PAYG 非零/额度不足时不搜索的门槛。此次未发搜索，无搜索 credit 消耗，不能声称已验证 Tavily 搜索可用性。

所有者需在 Tavily 控制台核对 Plan/Usage 的 PAYG 状态、已含 credits 余额及 API Key 限额；只提供状态，不提供 Key 或完整账户响应，不必充值或轮换密钥。账户变更与再次真实复验需另有明确授权，不从这次单次许可自动续跑。

## 剩余生产验收

普通 Runner 的公开访问拒绝/挑战、Z.ai 完整目录、Wayve 202 与 arXiv 429 仍在；NYU 身份/原文尾项、Tavily 搜索协议、完整固定批次与人物/非空中文写作、实际交付未通过。全部原入口仍为交付必需项，不下“本周无重要技术”的结论，不发送阶段性 memo。
