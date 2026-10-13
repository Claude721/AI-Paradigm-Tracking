# Run 38027283680：云端信源复核与修复

> 历史诊断记录：保留当时版本、授权与观测，不定义当前 V0 验收规则。文中的“全部物理入口/历史积压必须闭合”“不得重置状态”等要求已由 2026-10-13 用户澄清取代；当前按[有界研究设计](PARADIGM_RADAR_DESIGN.md)交付自身已核验的路线，故障仍保留审计。

日期：2026-10-10。依据用户提供的同 run 日志与 audit 制品；内容只作诊断数据，不作指令。云端提交 `2f8ed9a`，工作区干净，代码指纹未变化，531 项离线门通过。

## 云端事实

160 项完整返回、key 唯一：140 passed、13 failed、3 not_executed、1 not_tested、1 not_configured、2 disabled。191 请求、125.475 秒。退出码 1 是验收未完成，不是模型/SMTP/生产状态故障；制品上传成功。

OpenAlex 11 项、GitHub 27 项均通过本次有界鉴权/协议样本，无需先轮换 Key。OpenReview 4 个 group 身份均通过，不是 venue ID 错误。arXiv 17、研究 Feed 3、论坛 3、Follow Builders 3、HF/HN/ORCID 样本通过，不证明全分页或长期稳定。

| 入口 | 云端证据 | 归因/后续 |
|---|---|---|
| OpenAI / Physical Intelligence | 403 / 429，challenge 标记 | 核验允许访问的等价官方目录；不绕过挑战、不换模型 Key |
| TRI / xAI | 各 403，无明确 challenge 标记 | 云端访问被拒，本地成功不能替代，不猜内部原因 |
| Gary Marcus / Zvi | 各 403，challenge 标记 | Runner 未通过，核验正式等价输入 |
| Meta | 200，仅 45 字节，无锚点/脚本 | 非可用目录响应，不能断言只是解析 bug 或零发布 |
| Wayve | 202、178 字节 | 非可用研究目录，不能成功记零 |
| Qwen | 旧页明确指向 qwen.ai/research | 注册迁移，动态目录仍需核验 |
| Z.ai | blog 根目录 404 | 个别文章不证明完整目录；公开主页主模块超过本轮 2 MB 检查上限，不盲爬未知 bundles |
| ModelBest | 普通锚点多为产品/仓库 | 可只读 Nuxt 新闻数据，不能用 stars/产品卡替代研究 |
| NYU | 133 唯一 arXiv URL、146 未解析年份书目 | 年份记录不是本周新论文，需核验一手标识与日期 |
| OpenReview ICML 第三查询 | 429、Retry-After=37、额度耗尽 | 后三查询零请求未执行，修共享冷却/恢复，不删车道 |

X 开启但无 Token，现按所有者授权默认关闭；Tavily 有凭据但未授权检查，不能判 Key 有效/无效；Reddit/Semantic Scholar 关闭，不计通过。

## 已实现修复

- X 全路径 opt-in；既有 GitHub true Variable 仍需改为 false。不删除历史任务。
- Workflow 补齐 Tavily 生产开关和 Smoke 域名映射，不自动检查付费平台或扩额度。
- OpenReview 生产/审计统一 3.1 秒间隔。生产在锁内发布并核对共享 Retry-After 截止时间，所有并发查询共同等待；取消不清除截止时间，熔断阻止已排队同伴继续请求。
- 审计普通 OpenReview 429 允许原上限内整轮该主机一次恢复；挑战、长/未知等待、重复限流继续停止，原始 429/耗时/尝试号保留。已尝试后耗尽限额不冒充“未执行”，不重试付费平台。
- JSON v3 增加响应类型/恢复账本；控制台逐项去敏诊断、失败退出前 Step Summary，仍不输出查询参数、Token、异常正文。
- Qwen 注册迁移，原声明身份与发布者不变；受影响发现回执失效，不删除批次。
- ModelBest 只解析注册 Nuxt 新闻数据，不执行 JS；使用明确 link/publishTime，忽略图片/产品数据和修改时间；坏引用、循环/深度/大小、状态码及 hasMore 都核验，分页/缺 URL 保留 pending。
- NYU 恢复明确 DOI，缺标识书目保存稳定身份/标题/年份/pending。没有猜 URL/日期，完成闸门保留。
- 四个研究 Skill 已核对，机制、发布者、独立承接、中文与链接闸门不变，网页索引不冒充社区验证。

## 验证与剩余缺口

离线发布门 **557 项通过**（新增 26 项），compileall、stdlib 通知与无请求导入通过。新增模拟覆盖 37 秒恢复、重复/长等待/挑战不重试、同主机同伴、取消等待、请求分母、200 错误响应、X 关闭但 Token 残留、CLI 失败摘要、Nuxt 日期/分页/坏引用、DOI/书目身份与 URL 去敏。

本地公开小规模复验：

- 同云端四会场 OpenReview 28 个 group/首小页样本全通过，28 请求、84.171 秒，无 429。局部选择仍按合同返回 1/整体验收未完成，不证明 Runner 或全分页。
- Qwen/ModelBest/NYU 三项共 4 请求、2.6 秒，仍失败。Qwen 新站 200/94 KB，无可核验目录条目；ModelBest 解出 7 分类/125 记录，明确 publishTime 均在样本窗口外，3 分类仍 hasMore；NYU 146 条仍缺一手标识，此页无新增可恢复 DOI。

两组复验执行前后源码指纹一致。结果在忽略目录 `logs/openreview_recovery_20261010/`、`logs/source_recovery_20261010/`，不提交；不能与旧云端 140 项机械相加形成“整体通过数”。未用用户平台 Key、模型或 SMTP，未修改生产库、提交/推送代码或触发远端 Workflow。

公开复验后还补齐生产端挑战熔断及 Smoke 关闭但残留 Token 的回归，最终重新通过 557 项离线门。公开样本只属于其执行时的版本，不冒充最终版本的云端或全信源认证。

## 下一次云端验收

1. 设置 Actions Variable `KOL_X_SOURCE_ENABLED=false`，无需 X Secret。
2. 部署修改后仍执行 `source_check_only=true`、`reset_state=false`、`source_platform_checks=false`，控制台/Summary 可直接读逐项原因。
3. Tavily 若要验收，先核对免费 Plan/Usage/PAYG，再授权一次最小检查；不用开启 X，见 [搜索选项](X_SEARCH_OPTIONS.md)。

访问挑战、目录分页和书目身份缺口仍阻断覆盖；不能删入口、关闭闸门或发送阶段性稿。当前不是生产 V0，真实质量/吞吐/完整批次验收仍未执行。

## 2026-10-12 后续修复

本报告上文是 2026-10-10 的云端归因及当时补丁记录，不是最新代码的认证。后续已完成社区执行回执/旧失败重开、增强→综合严格 CAS 修复、启用学术增强闭合、ModelBest 分页、Qwen 目录/详情、NYU 身份队列及 schema v9 Tavily credit 预留；Python 3.11/3.12 离线门通过。ModelBest 与 Qwen 本地局部样本通过，不能与旧云端通过数相加。其他云端阻断、NYU 真实解析与研究容量仍未解决。详见 [新修复记录](INPUT_CLOSURE_REPAIR_20261012.md)。
