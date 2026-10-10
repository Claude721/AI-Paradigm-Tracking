# 逐入口信源验收

更新：2026-10-10。用于验证输入与接口，不是完整研究、报告或生产 V0 认证。它不会减少生产研究范围、清空 backlog 或把缺口改写为“没有更新”。最新结果见 [归因报告](SOURCE_ACCEPTANCE_20261010.md)，云端操作见 [Key 复验指南](SOURCE_API_KEY_VERIFICATION.md)。

## 云端先做什么

代码上传后，在 Actions → AI 技术范式雷达 → Run workflow 中设置 `source_check_only=true`、`reset_state=false`。该模式优先于 `smoke_only`，不恢复/写入生产状态，不调用模型、不登录 SMTP、不发送正式邮件或失败提醒、不自动排队其他研究；默认只使用已有只读 OpenAlex/GitHub 凭据。仍执行离线发布门。手动 `source_platform_checks` 默认 false；显式勾选才允许已启用的 Tavily/X/Semantic Scholar/已批准 Reddit 各做一次最小协议检查，可能消耗平台额度。

下载 `paradigm-radar-audit-<run_id>`：

- `source_audit_latest.json`：逐入口状态、协议/解析计数、HTTP 去敏元数据、临时 SQLite 回环及执行版本。
- `source_audit_latest.md`：便于逐项处理的摘要。
- `source_audit.log`：该次命令结果。HTTP 原始正文、查询参数、密钥与生产数据库不输出到验收制品。

生产研究源与辅助源按当前配置逐项列入清单，包括每个官方页面、KOL/RSS、论坛、Follow Builders 文件、学术查询和 GitHub 组织。官方页面最多一条详情、学术查询首个小页；GitHub/Search/作者查询等只验证协议，不声称全部 README、作者身份合并、独立承接或全分页已经完成。

## 本地命令与限制

脚本故意忽略 `.env`。不要把密钥放入命令行、聊天或文档；需要带凭据验收时由安全进程环境注入。

请求前复用零网络配置语法检查，不要求模型/SMTP 密钥；拼错布尔值不会静默变为 disabled，错误 venue、Feed 地址或超界参数会先明确退出，只显示变量名。

```bash
python scripts/source_audit.py --list
python scripts/source_audit.py --budget-seconds 900 --max-requests 300
python scripts/source_audit.py --include-authenticated --budget-seconds 900 --max-requests 300
python scripts/source_audit.py --entry official:5 --budget-seconds 100 --max-requests 6
```

默认输出 `logs/source_audit_latest.json/md`，已被 Git 忽略。默认 900 秒总预算、300 次总请求、45 秒单入口、6 次单入口请求；arXiv 同主机至少 3.1 秒，其他主机至少 1 秒。429 或明确配额耗尽后同主机剩余项停止请求，但其他来源继续；解压后 10 MB 上限和预算耗尽不记成功空结果。私网字面 IP、localhost、带 userinfo 和非 HTTP(S) 地址拒绝访问，重定向逐跳核对；不声称这是完整 DNS 重绑定防护。

`--include-authenticated` 仅开放有界 OpenAlex/GitHub。`--include-platforms` 是额外明确授权：Tavily basic 一次、X 最多 10 条的一次搜索、Semantic Scholar 一次、Reddit OAuth 加一次最小搜索；不启用配置中关闭的平台，不自动补格式或重试付费请求。未配置与 disabled 不算通过；启用入口缺少凭据也阻止 `acceptance_complete`。`--entry` / `--kind` 是局部诊断，始终不算完整验收；即使所选样本 passed，退出码仍为 1。

JSON v2 增加逐请求耗时、连接错误类型、限流/挑战提示和保守 `diagnosis`；不记录异常文本、响应正文、Token 或查询值。403 不能直接诊断 Key 失效。执行前后代码指纹变化也阻止完成声明。

重复观测使用下面的离线汇总，参数按先旧后新；不同 scope 不合并，缺席保留，时间缺失保持未知。不用少量样本估计 P95/长期可用率，不同代码版本的修复对照不是同版本稳定性实验。

```bash
python scripts/review_source_audits.py /首轮/source_audit_latest.json /复验/source_audit_latest.json --markdown
```

## 怎样读状态

| 状态 | 含义 | 可否认定生产覆盖闭合 |
|---|---|---|
| passed | 有界协议/生产解析样本通过；证据若在窗口内则经临时库回环 | 否，仍有详情/全分页/研究判断边界 |
| failed | HTTP、索引解析、字段或持久化契约失败 | 否 |
| not_executed | 时间/请求预算或主机限流阻止实际检查 | 否 |
| not_tested | 平台需单独授权、响应超样本大小等 | 否 |
| not_configured / disabled | 配置声明，未测 | 否，不算 passed |

所有清单项保持在分母；失败、未测试、未执行让验收非零退出。`acceptance_complete` 只表示声明样本清单已处理且无失败/未测试/未执行，`campaign_coverage_complete` 永远为 false。空合法 RSS、过滤出窗口外旧论文与“解析不到任何文章”的故障不可混为一谈。

## 2026-10-09 实测

根据新日志公开配置，使用四个 venue、Google/BAIR/OpenAI 三个 Feed 进行本地有界测试；未读取 .env，未调用模型、付费增强或邮件：160 清单项，102 passed、13 failed、3 not_executed、40 not_configured、2 disabled；150 次请求，约 106 秒。结果位于 `logs/source_acceptance_final_20261009/`，不提交到仓库。

| 未闭合入口 | 观察结果 | 后续验收要求 |
|---|---|---|
| OpenAI research、PI、TRI | 403 / 429 / 详情 403 | 云端验证等价一手入口，不能只凭 RSS 可访问宣称索引等价 |
| Z.ai blog | 根路径 404，个别文章可访问 | 找到完整可核验官方目录，不能靠固定旧文章 seed 替代 |
| Meta、Qwen、StepFun、ModelBest、Tencent ARC | 200 但无可识别研究条目，改版/动态列表 | 限定且可验证的数据适配器；不执行未知脚本或绕过鉴权 |
| Stanford CRFM、NYU CILVR | 根目录/书目结构没有被当前解析器识别 | 核对正式出版入口及条目正文/日期 |
| Wayve | 202 Challenge / 解析缺口；此前个别大 PDF 超样本上限 | 先分开索引访问与大文件样本，不能计零发布 |
| OpenReview ICML 后部查询 | 1 项 429，后 3 项同主机 not_executed | 云端分页/固定窗口恢复测试；无需重打健康页 |
| Gary Marcus / Zvi | 本地通过，新云端日志 403 | 必须用 Runner 结果验收，不能用本地 IP 覆盖云端事实 |
| OpenAlex/GitHub 及需密钥的平台 | 本地未配置/未测试 | 云端只读接口验收；可能付费的平台另行授权 |

NVIDIA GEAR 已修复公开脚本列表读取：46 卡片、45 条允许的项目/一手链接；最新样本首条是旧论文，详情解析后被日期窗口正确排除。该样本证明动态输入契约可用，不证明全部详情或本期空结论。Sakana/CMU 限定根路径、LILA 单正文模式和 Google 仅年份字段已修复；导航/修改时间不再成为文章或发布日期。

## 与最新事故的关系

`logs_102682886856.zip` 的 `9031490` 已通过云端旧 457 项测试、v8 恢复和状态上传，失败在研究/覆盖未闭合；原点仅 58/14,606、深挖返回 9/407，不是邮件发送失败。本轮增加 OpenReview 页级恢复、forum/count 契约、人物一次格式修复及输入协议检查，但没有证据证明整体吞吐已足够。

这两个同 run 制品现已补充，临时副本恢复守恒验证通过。真实问题还包括 10,770 条历史未审原点、过期来源仍被付费刷新、混合队列预筛碎片化，以及取消调用漏记。已做前置版本闸门、预筛缓存前填和取消审计修复；详见 [容量复核](RUN_37899070924_CAPACITY_REVIEW.md)。仍需 Runner 信源验收和授权的小样本真实模型实验。不能通过删 backlog、取消闭合门、重复扩预算或恢复阶段性邮件制造 V0。
