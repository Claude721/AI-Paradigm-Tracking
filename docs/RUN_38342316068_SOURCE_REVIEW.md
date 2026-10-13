# 新版同清单 Runner 验收

> 历史诊断记录：保留当时版本、授权与观测，不定义当前 V0 验收规则。文中的“全部物理入口/历史积压必须闭合”“不得重置状态”等要求已由 2026-10-13 用户澄清取代；当前按[有界研究设计](PARADIGM_RADAR_DESIGN.md)交付自身已核验的路线，故障仍保留审计。

2026-10-13，[run 38342316068](https://github.com/Claude721/AI-Paradigm-Tracking/actions/runs/38342316068)，job `115115839897`，提交 `1558771068e79731b3cd55fc61cd017078bb8bc4`。

仅信源检查：`source_check_only=true`、`reset_state=false`、`smoke_only=false`、`source_platform_checks=false`。没有生产状态恢复/保存、研究模型、报告、SMTP 或续跑调用。工作流 5m 3s，因未闭合输入正确失败，审计制品成功上传。

- 160 / 160 项：148 passed、8 failed、1 not_tested、3 disabled。
- 209 请求 / 188.758 秒；原清单 hash 仍为 `8cc40433dc2327fa65d7090d3d189021255d0b09f2b3f5f986b225458d1bb3fc`。
- 源码 `6918e95e00bc46f373df003aae22750031d8023936b13fd28fe5f1125891cbac`，前后不变、工作区干净；Runner Python 3.12.15 的 750 项发布门通过（98.426 秒）。
- `acceptance_complete=false`、`campaign_coverage_complete=false`。不同代码/样本边界的前后对照，不是长期稳定率实验。

## 修复生效与剩余故障

Meta 原入口仍为 200 / 45-byte 非内容响应并保留回执；同源出版目录和一篇实际详情通过，3 GET / 6.923 秒，parsed / persisted 1 / 1。仅首目录页 / 一篇详情样本，后续分页 pending 1、详情 pending 8，未把首屏冒充生产覆盖。本地完整 165 页与同窗恢复的独立证据见 [输入修复验收](SOURCE_REPAIR_20261013.md)。

| 必需入口 | Runner 实际观察 | 归因与边界 |
|---|---|---|
| OpenAI research | 403 / challenge | 公开访问挑战，不是 API Key 错误 |
| Physical Intelligence research | 429 / challenge，无 Retry-After | 不能当普通短限流反复请求 |
| TRI publications | 403 | 公开拒绝访问 |
| xAI news | 403 | 与关闭 X Bearer Search 是不同入口 |
| Z.ai blog 根目录 | 404 | 个别可访问文章不能替代完整官方目录 |
| NYU CILVR | 目录与一手详情均 200，但解析/身份未闭合 | 2,108 引用，1,808 明确更早年份排除，134 arXiv URL；145 未知引用段落合并为 138 个完全相同引用 SHA 的独立任务，7 个重复段落仍在原始计数；身份搜索关闭，不是本周 138 篇论文 |
| Gary Marcus | Feed 403 + 同作者 archive 403，两者均 challenge | 新 fallback 已执行，但 Runner 的公开归档也被拒绝；本地可用不能算云端通过 |
| Zvi | Feed 403 + 同作者 archive 403，两者均 challenge | 同上，保留全部失败回执与原入口 |

Tavily 仍为 not_tested：本轮未授权额外平台协议请求。X / Reddit / Semantic Scholar 的关闭声明不算通过。已启用 OpenAlex / GitHub 样本没有鉴权失败证据；不应因此轮换 Secrets，也不加大模型预算处理这些公开入口问题。

## 制品后续发现的真实代码缺陷

NYU 首个 arXiv `2606.32026` 实际 `<title>` 为带编号的浏览器标签，后面明确提供 `citation_title`。旧解析器先读浏览器标题，未覆盖为正式论文题名，导致精确身份核验必然失败；arXiv `citation_author` 又以 family, given 形式给出姓名，不能直接拿该顺序做名字首字母核验。

后续补丁优先使用唯一明确的 `citation_title`，冲突标题仍失败；仅对 arXiv 的 `citation_author` 作无损姓名顺序规范化，不猜 generic 作者列表、不放松精确题名/作者/原文日期核验。真实公开复验 2 GET / 3.107 秒：详情失败 1→0，窗口外已核验 0→1、剩余直接详情 133，未知身份 138 仍 pending。正确识别 June 30 的旧论文，不把首次入库当本周发布，也不把整个入口改绿。该补丁不在本次 Runner 的 `1558771` 中；后续 Python 3.11.15 / 3.12.13 各 753 项发布门通过，执行源码指纹 `1c685f1633c054a181ce3996f6b874aeabb8d8c280e976cc1fa48e0dc3ed214f`，局部实测和完整离线门不冒称新版全量 Runner 验收。

## 制品与结论

原始 ZIP `paradigm-radar-audit-38342316068.zip` 已下载到本机 Downloads，artifact `11787343019`，7 文件 / 730,679 解压字节，ZIP 114,941 字节，SHA256 `b4712b301005421796804a5e89d5455d27f090a4f288ba636a8fbf00306330a1` 与上传日志一致。原始日志/数据库不提交。

仍不是 V0。六个公开入口在 Runner 被拒绝、Z.ai 完整目录、NYU 身份/详情、Tavily 独立验收与人物/非空写作/完整批次仍需闭合。全部入口继续必需，不能靠删来源、解锁挑战、猜日期/身份或发阶段性 memo 达标。
