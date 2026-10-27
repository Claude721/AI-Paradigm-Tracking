# 本地研究 JSON 留存与邮件附件

按用户要求，审阅数据只写本地，以 ZIP 邮件附件寄出，**不上传新增的审阅数据 GitHub Artifact、不需要归档 Secret、不接入训练**。现有业务数据库/报告/去敏审计制品保持原有流程；这里替换的只是新增审阅数据的保存通道，不改变研究筛选、去重、全量深挖、正式报告和投递签名。

## 默认行为与边界

- `RESEARCH_ARCHIVE_ENABLED=true` 默认开启；`.private/research_archive/` 收集审阅 JSON。可显式设为 false 暂停留存；不删除已有数据。`RESEARCH_ARCHIVE_DIR` 仅本地可换成专用私人目录；正式 Workflow 固定为该私有路径。
- 正式 Workflow 在研究后独立执行 `mail_reviews`：打包本 run/attempt 的审阅数据，使用已有 SMTP 配置和 **现有 `SMTP_TO` 全部收件人**，发一封 `[研究数据留存]` 邮件。附件名为 `research-reviews-<run_id>-<attempt>.zip`，内部只有 `review-archive.json`。这是数据副本，不是阶段性研究报告，不代表研究或正式交付成功。
- 研究/报告/SMTP 普通失败时，后置步骤仍尝试发送已捕获数据。独立数据邮件失败在 Workflow 中告警，并在 Job Summary 标明“本轮数据留存未完成”，不把已交付周报翻成整轮失败，不回滚健康研究、正式报告签名或已发邮件，也不触发额外模型补跑。正式报告必需邮件失败仍使交付失败。预算自动续跑若已由用户开启，还须本轮数据留存步骤成功；默认仍关闭。
- Smoke、逐入口检查与独立单路线工作流不发送数据附件。本地正常运行只收集 JSON，不自动额外发送数据邮件；`mail` 是显式发信命令，未获授权不要执行真实发送。
- 旧 `RESEARCH_ARCHIVE_KEY`、`archive_initialize`、`archive_feedback` 不再使用，不必新增；若先前创建过归档 Secret，可在 GitHub 手动删除。模型 MAIN/SUB 两套 Key 不受影响。

**这里的本地有两个位置**：GitHub Runner 的临时磁盘和你电脑的长期档案。Runner 结束后不能依赖其磁盘，因此请下载邮件附件到自己的电脑；下一次 Runner 不会读取电脑上的文件/人工标签。历史累计与人工反馈保存在电脑，通过下述 `merge` 更新，不需上传或回传 GitHub。

ZIP 是普通压缩文件，**不是加密或数字签名**。邮件经过现有邮箱服务，会在收发邮箱留下副本，不是严格意义上的“完全不经过云端”。请核对 `SMTP_TO`，不要公开转发附件，也不要将原文、prompt、反馈或 ZIP 提交 Git。`.private/` 已忽略。没有新增研究/付费 API，但仍使用原有 SMTP；不自动更换收件人或读取私人邮箱。

## 保存什么、怎样关联

解包后的结构：

```text
origins/<origin_id>.json      原点身份、不可覆盖的内容版本、请求/判断索引
aliases.json                  已明确出现的 arXiv/DOI/OpenReview/原文 URL → origin_id
calls/<call_id>.json           SUB/MAIN 实际请求、可见回答、验证状态、用量、关联原点版本
standards/<hash>.json          当时 Rubric、skill 模板、研究配置、执行代码身份
judgments/<judgment_id>.json   规范化初筛、缓存复用、最终候选快照及请求引用
feedback.json                 人工偏好（唯一日常手动编辑入口）
capture-status.json           留存错误/运行身份；无材料正文或 Key
```

- 所有已审阅原点都留存，包括观察/淘汰，不只保存入选路线。捕获资格预筛、机制抽取（含 Technical Report 索引/分机制）、综合/修复与人物研究；纯报告排版写作、未绑定原点的 Smoke/legacy 调用不纳入该数据集。
- `calls[].request.messages` 是应用实际传给模型客户端的完整消息，包括已抽样/截断的正文、二次材料、修复提示，留存不会再次截断。它不是承诺抓到了整篇论文；`available_document_excerpt` 只表示当时可用节选。
- 保存当时标准、模型/提供方/服务地址和请求策略。协议适配的参数变化记录在 `transport_policy`。不保存 SDK 凭据、HTTP headers、环境变量全集、隐藏系统指令或 `reasoning_content`。已知密钥/常见认证字符串去敏，`redacted=true` 表示安全替换，不冒称字节级原文。
- 先保存请求，再执行原有调用；可见回答在 JSON/Rubric 校验之前保存。无效 JSON、重试、失败、取消和未捕获回答保持真实状态，不当作成功样本。`judgments` 是现有规则的规范化结果，**不是模型原话**；模型原始可见输出仍在 `calls`。没有进入 MAIN 不伪造 MAIN 判断，只有进入深挖也不自动成为最终正样本。
- 输出预算截断仍原样保存可见回答与 `finish_reason=length`。完整且验证可用的回答记 `validation.status=passed`；需要补全的记 `needs_repair`，不可当成技术负样本。后续请求保留原草稿、同一原点版本与独立请求 ID；补全成功不会覆盖第一次回答。隐藏 `reasoning_content` 不留存、不送入补全。这个请求级状态不是候选是否发表，也不代表重试一定成功。
- 原点使用明确 ID/URL，不按标题猜合并。内容变化追加版本，一报告多机制/多原点一路线通过 `origin_refs`、`candidate_key`、`mechanism_id` 和请求 ID 关联。本地合并若将补全 ID 的新批次映射到已有原点，会保留 `imported_origin_id`，不改原始 prompt/模型回答/判断结果。
- 候选快照中的 `excluded_evidence` 保留模型判为无关的原对象；它不是未经读取的新材料，也不提供报告事实。完整模型输入与原始输出仍按请求记录关联，执行失败样本不自动成为负标签，人工反馈与合并规则不变。
- 开启前/其他 Runner 的旧缓存不能反推原始输入。复用时有原始请求则引用；没有则明确缺失，不伪造旧 prompt/标准，不付费补跑。每个邮件是本轮增量，不冒称云端累计全文档案。

## 电脑上累计保存与人工修改

把邮件附件保存到私人目录，例如项目的 `.private/inbox/`。在项目根目录运行，用真实文件名替换示例：

```bash
python scripts/review_archive.py --directory .private/review-master merge --input .private/inbox/research-reviews-123-1.zip
python scripts/review_archive.py --directory .private/review-master view --output .private/label-view.json
```

以后每收到一个新附件，再执行一次同样的 `merge`。不存在的 master 自动创建；重复导入同一附件幂等，按明确 ID/URL 合并原点/内容版本，追加模型记录。**已经存在的本地人工反馈始终优先，不被邮件中的默认标签覆盖**；相同请求/判断 ID 却内容不同会拒绝，不悄悄改写模型历史。合并中断有本地事务快照，可在下次打开时恢复并保留当前人工反馈。不要同时编辑正在写入/合并的目录。

编辑 `.private/review-master/feedback.json` 中对应行，只改 `retain` 与 `note`，保留文件中的真实 origin_id、revision 与其他字段：

```json
{
  "schema_version": 1,
  "annotations": {
    "<文件内真实 origin_id，勿直接使用此占位符>": {
      "title": "原标题",
      "retain": false,
      "note": "我认为这只是工程增量，不留存",
      "revision": 0,
      "updated_at": null
    }
  }
}
```

`true`=人工留存，`false`=人工不留存，`null`=不覆盖系统标签。人工反馈不进入生产研究，不训练、不改变未来模型判断；重新运行只追加新判断，不能覆盖本地反馈。不要修改 `calls`、`judgments` 或内容版本。`view` 并列显示系统标签、人工反馈与 `effective_dataset_label`，只供离线查看；尚未完成最终判断的样本保留 null，训练前仍应按角色/阶段/验证状态自己筛选。

需要将独立编辑副本的反馈导入 master 时（通常直接修改 master 即可）：

```bash
python scripts/review_archive.py --directory .private/review-master feedback-import --input .private/edited-feedback.json
```

此命令按 revision 防冲突，相同值重复导入幂等；改变值成功后 revision +1，旧版本不能覆盖新反馈。不必把标签上传 GitHub。若从另一个已手改过的档案合并，已有 master 标签仍优先；要替换它请显式修改 master 或导入反馈补丁。

单独解包检查一个附件、不影响 master（目标目录须为空）：

```bash
python scripts/review_archive.py --directory .private/review-inspect unpack --input .private/inbox/research-reviews-123-1.zip
```

备份本地累计 master：

```bash
python scripts/review_archive.py --directory .private/review-master pack --output .private/review-backup.zip
```

这些离线命令不访问研究 API、不发邮件。不要手动用新包覆盖整个 master 目录，那会绕开保留人工反馈的合并规则。

## 发信与持久性限制

显式数据发信命令（仅在授权后使用）：

```bash
python scripts/review_archive.py mail --output .private/mail/research-reviews.zip
```

使用现有 `SMTP_*`/`EMAIL_PUSH_ENABLED` 配置，所有现有收件人必须被 SMTP 接受，否则不标记成功。ZIP 大小受现有 `EMAIL_MAX_ATTACHMENT_BYTES` 限制，超限明确失败、不截断正文、不偷偷遗漏样本，也不改用 GitHub 上传。独立数据邮件失败不撤销已经完成的正式报告交付。

只有 Runner 收到 SMTP 接受回执，仍不能保证收件箱最终入箱，请检查垃圾箱/附件并尽快下载备份。Runner 硬取消、总超时、SMTP 拒收/附件超限、机器故障可能导致本轮数据未寄出；该方案没有第二个云端备份通道，Runner 销毁后无法恢复尚未寄出的新内容。不要为了补发丢失附件盲目重跑付费研究。旧批次数据在收到并保存的邮件/本地 master 中继续保留。

单包解压 JSON 上限 512 MiB，超过上限拒绝、不删除历史；本地累计 master 达到上限需另行分卷归档。ZIP 只做结构/摘要及关联检查，不宣称密码学防篡改。保存内容仅限原有研究实际读取的材料，不新增抓取、公开再分发或训练许可。
