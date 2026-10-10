# 2026-10-10：逐入口验收与归因

结论：本地完整清单已做有界检查和修复后复验，**尚未全部通过，更不是生产 V0 认证**。最终 160 项：110 passed、8 failed、40 本地未注入凭据、2 disabled；158 次请求、约 109.7 秒。没有真实模型、SMTP、生产状态写入或付费平台请求；未推送代码或触发远端 Workflow。

清单使用内置入口，以及四会场（ICLR 2026、NeurIPS 2026、ICML 2026、ICLR 2025）和 Google/BAIR/OpenAI 三个公开 RSS 配置样本。不能据本地样本证明远端最新 Variables 相同。生产解析、临时 SQLite 回读均经过检查，所有失败/缺凭据项仍在分母。

原始制品在忽略目录 `logs/source_acceptance_20261010_accepted/`：`source_audit_latest.json/md`、`source_audit_review.json/md`。执行前后代码指纹一致。修复前 13 failed/102 passed 与最终结果属于不同代码版本，不是长期稳定性实验，不估计 P95 或长期成功率。

## 逐类结果

| 输入能力 | 清单项 | 最终结果 |
|---|---:|---|
| 官方研究页面 | 42 | 34 样本通过，8 失败 |
| KOL Feed | 19 | 19 通过；云端 IP 差异待验收 |
| 官方 RSS/Atom | 3 | 3 通过 |
| LessWrong / Alignment Forum | 3 | 3 通过；选择条件不是精确 karma |
| Follow Builders | 3 | 3 通过 |
| HF Daily Papers | 1 | 1 通过；不单独生成范式 |
| arXiv 精确 ID/各召回查询 | 17 | 17 小页样本通过 |
| OpenReview group/各查询 | 28 | 28 通过；前一轮曾出现 429，不代表长期稳定 |
| Hacker News / ORCID | 2 | 2 协议样本通过 |
| OpenAlex Works / Authors | 11 | 本地没有 `OPENALEX_API_KEY`，未测 |
| 官方 GitHub 组织 / Search | 27 | 本地没有 `GITHUB_TOKEN`，未测 |
| Tavily / Semantic Scholar | 2 | 本地没有相应 Key，未测 |
| X / Reddit | 2 | 本地关闭，未测，不记通过 |

“通过”不证明全分页、所有详情、身份合并、独立承接、模型判断、长期 SLA 或当前窗口没有发布。

## 本轮修复

- CRFM：主页不是研究列表，保留原声明身份而读取官方 `/blog.html`；注册内部文章路径。61 个识别链接、48 个允许链接，详情样本解析成功；没有元数据日期仍保持未知，不猜 URL 中的日期。
- StepFun：旧 `/research/en` 不是目录，读取 `/research` 的公开 title/path/date 卡片，仅接受注册 en/zh 文章路径。识别 14 条，均为窗口外旧材料；不据此证明公司本期没有其他发布。
- TRI：支持完整 `03 August 2026` 等卡片日期，单独年份仍未知。旧首项不再误选并跳转到 Wiley 403；下一条详情样本成功，但也是窗口外旧材料，不证明所有出版商页面可读。
- 腾讯 ARC：只读页面已引用的同源 index → 唯一注册 Research 模块，提取 14 个论文卡片；不执行 JS、不抓临时签名图片。显式 arXiv PDF ID 先确定性规范化到并核验 abs 页，保留原 PDF URL，避免入口检查下载超过 10 MB 的文件。首个样本旧日期已核验；全文 hydration 仍属于研究阶段。
- WordPress/NYU：修复 `content-sidebar-wrap` 被误当旁栏、导致整篇正文丢失的问题。明确 arXiv ID 可以恢复，只有年份不伪造日/月。2,108 条引用记录中，1,808 条明确早于窗口起始年份，排除依据仍记录；其余得到 133 个唯一 arXiv URL，146 条窗口年份书目缺少一手标识/链接，故入口仍未闭合。这些是目录记录，不是本周新论文或研究完成回执。
- 来源适配规则进入发现计划签名，旧覆盖回执不能静默复用；未解析书目参与完成闸门和失败来源重试，不能藏在 completed 汇总中。没有删除已有批次任务、扩大历史窗口或改生产库。
- 审计增加逐请求耗时、连接错误、限流/挑战提示及保守归因，401/403/429/额度/解析分账；不输出错误正文或 Key。缺凭据不算完成。离线多轮汇总保留 scope 与缺席项，不冒充稳定性统计。
- 新增默认关闭的 `source_platform_checks`：显式授权后才对已启用平台做最小鉴权样本，不默认付费、不调用模型或邮件。

## 最终失败的 8 个入口

| 入口 | 观测与归因 | 是否先换 Key | 后续处理 |
|---|---|---|---|
| OpenAI research/index | 403，明确 challenge 响应头 | 否 | Runner 复核访问，寻找完整等价官方目录；新闻 RSS 不能冒充全部研究覆盖 |
| Meta publications | 200，仅 45 字节，无可用目录 | 否 | 区分特殊响应/访问策略与目录协议，不能断言只是解析 bug |
| Physical Intelligence research | 429，同时有 challenge 响应头 | 否 | 不只是普通速率问题；保留挑战故障，验证正式公开等价输入 |
| Wayve science | 202、179 字节，非研究列表 | 否 | Runner/官方入口复核，不把异步或挑战响应当零发布 |
| Qwen 旧 publication 页 | 明确迁移到 qwen.ai/research；新目录仍无可核验条目 | 否 | 需要稳定公开数据适配，不因找到迁移 URL 就计完成 |
| Z.ai blog 根目录 | 404；个别文章可读不证明目录完整 | 否 | 找完整可核验目录，不能固定 seed 旧文章或用产品 sitemap 替代 |
| ModelBest 首页 | 产品/GitHub/HF 卡片，没有符合契约的研究条目 | 否 | 需要发布目录或代码原点能力适配，首页/星数不直接生成范式 |
| NYU CILVR publications | 正文和 arXiv 部分已修复，146 条窗口年份书目仍无一手链接 | 否 | 继续有来源支撑的 DOI/出版元数据核验，或在许可下做学术身份检索；不猜 URL、不删记录 |

本轮已核对可读目录与页内公开数据，没有绕过访问挑战。仍缺完整等价覆盖证明的入口保留失败，不擅自删源或改变声明范围。外部访问、目录协议和缺一手链接的书目不是都能靠一次本地补丁消除。

## 凭据与下一步

没有证据证明现有云端 Key 失效：本地缺少凭据不等于远端 Key 错误。按 [Key 复验指南](SOURCE_API_KEY_VERIFICATION.md) 部署后执行 `source_check_only=true`、`reset_state=false`。需要平台最小检查时显式勾选 `source_platform_checks=true`，可能消耗 Tavily/X 额度；下载去敏制品，不提供密钥值。

Runner 用于验证真实 Variables、凭据和云端 IP/CDN 差异。不要直接运行有模型费用的完整研究来探测输入。输入样本通过后仍需处理率、固定窗口恢复与真实研究质量验收。继续禁止阶段性报告，不用故障伪造“本周没有重要技术”。

## 离线验证

`scripts/offline_checks.py`：531 项测试、stdlib 故障通知、无请求探针导入与 compileall 通过。50 轮故障注入只压我们的代码（100 次 MockTransport 请求），覆盖 429、403、200 登录页、连接失败和坏 Feed，验证健康同伴入库、清单守恒。另有目录/脚本同源、日期、规范化 URL、书目年份界限、来源回执换版、完成闸门、最小鉴权/Key 去敏、X 假空结果与缺席 scope 回归。Workflow YAML 和 `git diff --check` 通过。
