# 信源 API Key：安全复验指南

更新：2026-10-13。本地不复制用户云端凭据；已授权 Runner 证据优先。公开网页的 403、429、404、访问挑战和解析错误不是模型或信源 Key 无效的证据。**先验收注入与接口，再决定是否轮换，不要整体重置密钥。**

最新 [独立验收 run 38370843944](RUN_38370843944_INTERFACE_REVIEW.md)：NYU 8 次 OpenAlex 查询全部 200，Tavily /usage 200，严格 included-only 保护因 PAYG 限额未知停止，未发搜索。没有需要先轮换这两个 Secret 的证据。用户随后要求暂不调用，不能自动复验。控制台 0/1000 说明当时月度余额充足，但不证明搜索已执行；该独立费用保护不应成为整个研究流程卡点。无需先充值或重配 Key，正式研究可从其他健康通道继续。

较早的 2026-10-10 云端 run `38027283680`：OpenAlex 11 项、GitHub 27 项通过，不需先轮换这两类凭据。X 无 Token，所有者决定暂时关闭，将 Actions Variable `KOL_X_SOURCE_ENABLED=false`（既有 true 不会被新默认覆盖），不用申请 X Secret。当时 Tavily 未测试，使用免费额度前先核对 Plan/Usage/PAYG，见 [X 搜索选项](X_SEARCH_OPTIONS.md) 与[当时云端复核](RUN_38027283680_SOURCE_REVIEW.md)；之后 usage 200 的证据见上段。

## 1. 在 GitHub 验证已有凭据

只授权 NYU 书目身份或 Tavily 时，改用独立 `source-interface-acceptance.yml` 的 `scope=nyu/tavily/both`，不要勾选全平台检查后误测其他已启用接口。它默认不执行，只在授权 API step 注入选中凭据，无模型/邮件/生产状态；Tavily 先核验已有包含额度与 PAYG 关闭，条件未知时不搜索、不改套餐。2026-10-13 所有者已授权最多 8 次 NYU 身份查询和 1 次 Tavily basic，需新增付费则停止；结果另列。最新 [run 38347140838](RUN_38347140838_SOURCE_REVIEW.md) 中 OpenAlex 11 项通过，摘要两条 Key/用量异常来自离线 fixture 污染，不能据此轮换 Secret。详见 [独立接口验收](SOURCE_ACCEPTANCE.md#独立-nyutavily-接口验收)。

1. 先部署本轮代码到要验收的分支。确认 Actions 的执行提交和 `source_audit_latest.json` 中代码指纹；旧工作流没有新检查选项。
2. 仓库 Settings → Secrets and variables → Actions，核对下表中的 Repository Secrets 名称。只核对名称/提供方账户，不截图或导出值。Environment/Organization Secrets 还需确认该 job/仓库确有访问权限；不能仅凭“设置页面存在”认定注入成功。[GitHub 官方 Secrets 说明](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)
3. Actions → AI 技术范式雷达 → Run workflow：`source_check_only=true`、`reset_state=false`。`smoke_only` 可以保持默认 true，信源检查优先；不会检查真实模型、SMTP、恢复或覆盖生产数据库。
4. 默认 `source_platform_checks=false`，只读检查 OpenAlex/GitHub 和公开入口。若要验收**当前已启用、已经取得使用许可**的 Tavily/X/Semantic Scholar/Reddit，明确勾选 `source_platform_checks=true`：Tavily 最多一次 basic 搜索，月度保护开启时先 `/usage`；X/Semantic Scholar 各一次，Reddit OAuth 加一次搜索。Tavily/X 可能消耗额度，不自动启用关闭的平台或升级套餐。
5. 下载 `paradigm-radar-audit-<run_id>`，查看 `source_audit_latest.md/json`，尤其 `credential_name`、`missing_credentials`、HTTP 状态与 `diagnosis`。检查失败时 Actions 红色是保留缺口，不是发信故障。不要只凭总体绿色推断研究 V0。

## 2. 每个凭据怎么处理

| 接口 | GitHub 名称 / 配置 | 复验方法与失败处理 |
|---|---|---|
| OpenAlex Works / Authors | Secret `OPENALEX_API_KEY` | 到 OpenAlex Settings/API 核对当前 Key。项目检查 Works 与 Authors；`not_configured` 查注入，401 查 Key，429 查日预算/频率，不先轮换。[官方鉴权与额度说明](https://help.openalex.org/api/authentication/) |
| GitHub 组织 / Search | 自动 `github.token` → `GITHUB_TOKEN` | 当前工作流自动注入，不需要另建同名 Repository Secret 或临时扩大写权限。本地 PAT 与 Actions 临时 Token 不同；403 先看额度/权限，单个组织 404 先核对登录名和可见性。[官方限流处理](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api) |
| Tavily | Secret `TAVILY_API_KEY`；Variables `TAVILY_SOCIAL_SEARCH_ENABLED` / `TAVILY_MONTHLY_CREDIT_LIMIT` | 先核对账户 Plan/Usage/PAYG。默认上限 1000，先一次只读用量，再最多一次 basic 搜索；usage 401 查 Key/权限，用量未知不搜索，额度耗尽不是 Key 无效。429 查速率，432/433 查套餐/按量上限，不自动加钱。[官方 Usage](https://docs.tavily.com/documentation/api-reference/endpoint/usage)、[Search/错误码](https://docs.tavily.com/documentation/api-reference/endpoint/search) |
| Semantic Scholar | Secret `SEMANTIC_SCHOLAR_API_KEY`；Variable `SEMANTIC_SCHOLAR_ENABLED` | 使用提供方发送的 Key，检查 `x-api-key` 路径；一次 paper/search。429 不等于失效，不用匿名成功替代 Key 验收。[官方 API](https://api.semanticscholar.org/api-docs/) |
| X Recent Search | Secret `TWITTER_BEARER_TOKEN`；Variable `KOL_X_SOURCE_ENABLED` | 使用 App 的 app-only Bearer Token，不是 API Secret 或用户 Access Token。核对该 App 的 Recent Search 权限与账户额度；一次搜索，最多 10 条。[Bearer Token](https://docs.x.com/fundamentals/authentication/oauth-2-0/bearer-tokens)、[Recent Search](https://docs.x.com/x-api/posts/search-recent-posts) |
| Reddit | Secrets `REDDIT_CLIENT_ID`、`REDDIT_CLIENT_SECRET`；Variables `REDDIT_USER_AGENT`、`REDDIT_API_ACCESS_APPROVED` | 必须先确认项目用途已获相应批准；一个 OAuth 请求和一个最小搜索请求。布尔开关不是平台批准证明，认证通过也不是用途许可证明。[官方接入说明](https://support.reddithelp.com/hc/en-us/articles/14945211791892-Developer-Platform-Accessing-Reddit-Data)、[OAuth/User-Agent](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki) |

配置名称来自当前工作流。不要为验收去开启原本不用的付费平台。DashScope 模型 Key/SMTP 密码不在本次信源检查范围；已成功研究/发送的历史记录也不能证明所有信源 Key 有效。

## 3. 按结果决定动作

- `not_configured`：只说明这次进程缺少对应配置。先检查 Secret 名称、环境访问、工作流映射、变量开关；不需要立即重新申请 Key。
- `authentication_rejected` / HTTP 401：核对提供方账户中当前有效 Key、粘贴时空白、Token 类型和是否已撤销。确需更新时，在 Actions Secret 的 Update 中填写新值，重新执行同一有界检查；不要把值写到 Variables、命令行参数或日志。
- `authenticated_access_denied` / HTTP 403：先区分权限/套餐、提供方 IP 规则和限流。不能仅凭 403 断言 Key 错误。
- `rate_limit_or_quota` / HTTP 429：等冷却/重置，检查额度，不连续重跑。已限流同主机入口保持未执行，不用“跳过”冒充通过。
- `provider_access_challenge`、公开页面 403/202：通常没有对应 API Key 可换；保留故障，核验正式公开的等价输入，不绕过访问挑战。
- `index_structure_or_wrong_catalog`：网页可访问但没有可识别研究条目，需要目录/数据适配，不是鉴权修复。

本地复验脚本故意忽略 `.env`，需用安全进程环境注入凭据；不建议为了这次验收把云端密钥复制回电脑。最省事且最接近生产环境的做法是使用上面的 Runner 模式，只回传去敏审计制品。
