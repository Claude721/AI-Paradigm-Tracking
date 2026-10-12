# 2026-10-12 Runner 信源验收

后续新版同清单复验见 [run 38239955723](RUN_38239955723_SOURCE_REVIEW.md)：Wayve 云端通过，整体仍有 9 失败/1 未测试。本文保留首轮 10 失败的历史事实，不以新结果覆盖。

已按用户授权将 `81b89468259d37c20dbc59daf5e585458fffcf28` 推送到 `main`，并启动 [run 38232622479](https://github.com/Claude721/AI-Paradigm-Tracking/actions/runs/38232622479)。参数：`source_check_only=true`、`smoke_only=false`、`reset_state=false`、`source_platform_checks=false`。没有生产状态、研究模型、SMTP 或续跑调用。

Runner Python 3.12.15，源码指纹 `7418c291a71993371b640fb14d9ce0d9c626b6c4c079af9ad86574f778d0f8d2` 与本地 657 项发布门一致，云端发布门也通过。160/160 项返回：146 passed、10 failed、1 not_tested、3 disabled；205 请求、183.876 秒。工作流因输入未闭合正确失败，不是成功空研究。原始 ZIP/日志不提交。

| 失败入口 | 本次事实 | 后续处理 |
|---|---|---|
| OpenAI 研究目录 | 403 访问挑战 | 需要公开等价完整目录，不以新闻 RSS 冒充研究全集 |
| Meta publications | 200，仅 45 bytes，无目录 | 未取得可用目录，不算零发布 |
| PI research | 429 访问挑战 | 不压力重打、不绕过挑战 |
| Wayve science→labs | 目录 200，17 条链接；首条 PDF 超过 10 MB | 下载大小限制需修复；17 条任务仍保留 |
| TRI publications、xAI news | 403 | 需要 Runner 可访问的等价官方输入 |
| Z.ai blog | 404 | 完整目录迁移未核验，不替换成固定已知文章 |
| NYU bibliography | 一手身份未解析 | 解析默认关闭，pending 保留，不静默消耗额外搜索 |
| Gary Marcus、Zvi Feed | 403 访问挑战 | 覆盖缺口保留，不计作零讨论 |

arXiv 精确 ID、领域/作者/报告车道、OpenAlex Works/Authors 与 GitHub 组织/搜索样本全部通过。本次没有鉴权错误证据，不需要据此重新生成 Key；样本不证明生产全分页稳定性。Tavily 未验收，X/Reddit/Semantic Scholar 关闭，均不计作 passed。

后续只读 HEAD 核对 Wayve 官方 PDF：`Content-Type: application/pdf`、`Content-Length: 29331554`。原验收器在 10 MB 截停，尚未进入 PDF 解析。新版生产官方页与验收共用流式解压上限：普通响应 10 MB，真实 `%PDF-` 签名文件 40 MB；不能仅凭扩展名/MIME 升级。超限关闭连接并保留失败，独立归因为大小限制。PDF 页数/文字、日期、链接与研究质量门槛不变。

上述修复晚于此 run；不能冒称旧 run 已通过新版。随后本地真实公开 Wayve 有界复核：3 GET、8.119 秒，29,331,554 bytes PDF 全部读取、解析并临时入库回读 1 条；0 detail_failures，仍保留 16 条未尝试详情。源码指纹前后相同 `f32f836eb9961098f0e1bb1e25507d4573c74bd7e68c2c9397c8cd63257163be`。该选项样本 passed，但完整验收 false、退出 1；没有将 17 条详情冒称闭合。该版本 Python 3.11/3.12 各 679 项发布门通过，耗时 57.621/57.284 秒。这不是新版 Runner 全清单认证。随后 [状态 v9 实库副本迁移](STATE_V9_REPLAY_20261012.md) 已通过；[首轮真实模型](RUN_38235929382_MODEL_REVIEW.md) 未闭合，质量及整批交付仍需验收，当前不是生产 V0。
