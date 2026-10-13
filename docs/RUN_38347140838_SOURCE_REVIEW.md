# 最新代码 Runner 复验与测试输出污染

> 历史诊断记录：保留当时版本、授权与观测，不定义当前 V0 验收规则。文中的“全部物理入口/历史积压必须闭合”“不得重置状态”等要求已由 2026-10-13 用户澄清取代；当前按[有界研究设计](PARADIGM_RADAR_DESIGN.md)交付自身已核验的路线，故障仍保留审计。

2026-10-13，[run 38347140838](https://github.com/Claude721/AI-Paradigm-Tracking/actions/runs/38347140838)，job `115131066962`，执行提交 `60058a5ac372e0f4e9bde03e225d34cfd53e302f`。`source_check_only=true`、`reset_state=false`、`smoke_only=false`、`source_platform_checks=false`；只读核对书目开关未配置，默认 false，未修改 Variables/Secrets。

工作流 4m 29s，job 4m 25s；因输入缺口正确失败，制品上传成功。研究、邮件、生产状态、预算续跑均未进入。

- Python 3.12.15 的 771 项完整离线门通过（104.467 秒）。
- 源码 `72d35017bc98a0feb5e39e22826bb22208e66b87cd20915c133ed1d59cc6d433`，工作区干净、前后不变。
- 160/160：131 passed、10 failed、15 not_executed、1 not_tested、3 disabled；192 HTTP / 147.161 秒。
- 清单 hash `8cc40433dc2327fa65d7090d3d189021255d0b09f2b3f5f986b225458d1bb3fc` 不变，不删入口或未执行项。

## 补丁生效与实际新增波动

NYU 首篇 arXiv 200，正式题名/作者/日期对齐：0 详情故障、1 条明确窗口外、133 详情尾项；138 个独立身份任务均 `resolution_disabled`，没有新增 OpenAlex 身份查询。学术元数据补丁已在 Runner 生效，不再只依赖本地证明；其余身份/详情没有据此改绿。

Meta 首目录/最多一篇详情再次通过（3 GET），完整目录/详情尾项仍保留，首屏不等于覆盖完成。OpenAlex 11 项仍全部 passed，没有 Key 失效证据。

此前 8 个未闭合入口仍存在：OpenAI / PI / TRI / xAI / Gary / Zvi 被拒绝或挑战，Z.ai 根目录 404，NYU 身份未闭合。新增两项波动：Wayve 索引重新出现 202 / 179-byte 非正文，尚未请求 PDF，不能归因为已修的 40 MB 下载上限；arXiv 精确 ID 样本通过，但首个领域查询 429、无 Retry-After，后续 15 查询停止并保持未执行。没有高频重试或伪造成功空结果。

## 摘要中的两条假接口失败

真实 Actions Step Summary 曾出现 `OPENALEX_API_KEY_not_configured` 与 `execution_error_usage_unknown`，但它们来自新增离线用例模拟 `source_interface_check.main()` 的失败结果，并非本轮真实接口请求或真实用量未知。正式 SourceAudit JSON 与 HTTP 账本才是本轮输入证据，不能根据这两条测试提示轮换 Key。

原因：离线子进程继承父 job 的 `GITHUB_STEP_SUMMARY`，模拟 CLI 因此写进真实摘要。修复隔离五个 Runner 写入通道：`GITHUB_STEP_SUMMARY`、`GITHUB_OUTPUT`、`GITHUB_ENV`、`GITHUB_STATE`、`GITHUB_PATH`；只读代码身份保留。CLI 失败测试也显式使用自己的临时摘要。新增真实子进程回归核对父 job 文件逐字不变及 fixture 显式临时通道仍可用。这个后续补丁不在本次 `60058a5` 中，最终离线门/后续独立接口 run 单独记录。

独立接口凭据进一步只在已授权 API step 注入，不交给依赖安装/离线测试；Tavily 额度/PAYG 保护使用固定原因码，不把合法停止错误归成鉴权或解析故障。生产研究范围与普通信源验收默认预算不变。

## 制品与下一步

ZIP `paradigm-radar-audit-38347140838.zip` 已下载到本机 Downloads：115,782 bytes，7 文件 / 730,539 解压字节，SHA256 `6b58c77c84935dfdeb53873848891a374a8f29c26abb1f3ecf761621b5823b05` 与上传日志一致，artifact `11789627161`。原始 ZIP/日志不提交。

所有者随后明确授权 NYU 至多 8 次 OpenAlex 身份查询、Tavily 至多 1 次 basic（已有 credit、需新增付费则停止）；不改 Variables、不调用研究模型/SMTP/正式研究。先发布上述隔离补丁，再用独立工作流的 `scope=both` 执行一次；授权不等于已执行或已通过，结果另列。不对未执行的 arXiv 或被挑战入口不停重跑。

仍不是 V0：当前输入与授权范围内的协议证据不能替代完整批次、人物、非空写作与交付验收。
