# 生产状态副本 v8→v9 迁移证据

> 历史诊断记录：保留当时版本、授权与观测，不定义当前 V0 验收规则。文中的“全部物理入口/历史积压必须闭合”“不得重置状态”等要求已由 2026-10-13 用户澄清取代；当前按[有界研究设计](PARADIGM_RADAR_DESIGN.md)交付自身已核验的路线，故障仍保留审计。

2026-10-12，从用户提供的 run 37899070924 对应状态 artifact 下载 ZIP，SHA256 核对一致：`c7960c35fe200a1066c70e37471b2bd3516e952d0ea501bb11ffb1eb811c179e`。metadata：schema 8，commit `9031490bbc62a6872b4adc2db2a6f96295fd4398`，landscape `2026-08-15.1`。仅将确切两项 `paradigm_radar.db` / `state-meta.json` 解压到新建临时目录；原 ZIP 和线上状态没有修改。

实际生产迁移函数在 439,181,312 bytes 的副本执行，返回 schema 9，耗时 1.618 秒。迁移前后 SQLite `PRAGMA quick_check` 均 `ok`。对以下所有原表按整行序列化计算 SHA256，数量及逐行摘要全部相同，不只是抽样或只比较总行数：

| 表 | 迁移前 / 后行数 |
|---|---:|
| evidence_state | 15,942 / 15,942 |
| paradigms | 451 / 451 |
| paradigm_evidence | 12,727 / 12,727 |
| report_deliveries | 11 / 11 |
| origin_research_state | 14,812 / 14,812 |
| research_campaigns | 1 / 1 |
| campaign_work | 15,066 / 15,066 |
| research_attempts | 118 / 118 |
| research_stage_cache | 63 / 63 |
| report_outbox | 7 / 7 |

新增 `provider_credit_periods` / `provider_credit_requests` 均为 0 行，未伪造历史扣费。没有模型、平台搜索、邮件、线上状态写入或清库操作；数据库、ZIP、日志不提交。原始本地诊断为 `/private/tmp/radar-state-v9.d25xDg/result.json`，临时目录不保证长期保存。

该证据关闭“schema v9 真实大小状态迁移未验收”的缺口，不代表 15,066 研究任务已完成，不替代领域状态恢复、同窗续跑、去重、输入全分页、报告质量或真实交付验收。既有 [同 run 容量分析](RUN_37899070924_CAPACITY_REVIEW.md) 仍用于积压组成与模型吞吐诊断。
