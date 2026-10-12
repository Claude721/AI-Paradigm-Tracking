# X 搜索选项与费用边界

核验：2026-10-10。所有者决定暂不使用 X 官方 API，代码、示例配置及 Workflow 默认 `KOL_X_SOURCE_ENABLED=false`，同时控制 KOL 原创、社区标题搜索、legacy Twitter 和 Smoke。即使保留 Token，关闭开关也不请求官方接口。

GitHub 已有 Variable 优先于默认值：Settings → Secrets and variables → Actions → Variables，将 `KOL_X_SOURCE_ENABLED` 明确设为 `false`。不需申请/轮换 X Secret，不删除历史研究任务或证据，新范围记录“未执行官方 X 搜索”。

| 方法 | 接入/费用 | 可证明的范围 |
|---|---|---|
| X 网页高级搜索 | 官方说明要求登录，可人工按词、账号、日期过滤 | 不是供无登录 Runner 使用的公开 API；不自动抓登录 Cookie |
| Tavily basic 网页索引 | 官方每月 1,000 免费 credits，无需信用卡；basic 每次 1 credit | 已有 SocialWebSearchClient 能发现被索引的 X/社区页面，不能保证全部帖子、完整正文或官方互动量 |
| Follow Builders 公共 Feed | 当前适配器无需 Secret | 上游精选二次传播，不是全 X 搜索；继续核验主体、相关性与独立性 |
| X 官方 API | 当前按 credits 使用量计费；有条件的赠送 credits 不是永久免费套餐 | 暂时关闭，不绑卡、不充值、不启用自动扣费 |

Tavily 的免费套餐事实不证明当前 Key 所属账户免费。先查看 Plan/Usage，关闭 PAYG 或设置账户硬限额，再授权检查。2026-10-12 新增 `TAVILY_MONTHLY_CREDIT_LIMIT=1000`：生产账户用量核验与 schema v9 持久化预留支持跨 run 保护，未知扣费不退款；`0` 明确关闭保护。它不能原子约束同账户其他客户端/计数延迟，不能替代提供方硬限额。`TAVILY_REQUEST_SAFETY_LIMIT` 仍只是每进程请求数熔断。独立平台样本先只读用量再最多一次 basic 搜索；`source_platform_checks` 默认 false，不因找到免费套餐就自动消耗额度。

正式检索域名由 `TAVILY_DISCOVERY_DOMAINS` 控制；留空可全网发现，只想找 X 索引线索时可由所有者明确设为 `x.com,twitter.com`，但这会缩小社区检索范围。`TAVILY_SOCIAL_SEARCH_DOMAINS` 仅用于平台 Smoke 样本，不替代正式范围。

本轮未使用用户 Tavily/X 凭据，未请求真实平台搜索。没有引入匿名抓取、Cookie/访客 Token 绕过或不稳定镜像作为 V0 依赖。索引线索不计独立承接，不猜互动量或“零讨论”。

官方依据：[X 高级搜索](https://help.x.com/en/using-x/x-advanced-search)、[X API 计费](https://docs.x.com/x-api/getting-started/pricing)、[Tavily 免费额度与 basic 计费](https://docs.tavily.com/documentation/api-credits)。
