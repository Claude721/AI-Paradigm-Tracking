# AI 技术范式雷达

这个项目不再以 GitHub 项目或 Product Hunt 产品为基本单位，而是每周捕捉正在形成的 AI 技术范式，并从技术反向锁定关键研究者。

目标只有一个：识别一个同时具备**清晰机制、扎实证据、足够外延空间和现实传播势能**的新范式。纯 benchmark 提分、狭窄组件替换、微小效率优化不会因为作者把问题讲得宏大就进入报告；团队背景无法核验、又没有独立讨论或承接的工作会先留在观察池。

## 新的工作流

```text
论文 / Technical Report / 官方研究博客
          ↓
 抽取一个或多个“新机制假说”
          ↓
 新颖性 + 扎实度 + 范式外延硬门槛
          ↓
 发布团队背景 × GitHub / HN / X / KOL 外部响应
          ↓
 OpenAlex / S2 / ORCID / 个人主页核验关键人物
          ↓
 研究总编辑合并技术路线并生成每周 Memo
```

信源被分成三类，职责不能混用：

| 层级 | 信源 | 用途 |
|---|---|---|
| 原始发现 | arXiv、OpenAlex、OpenReview、官方 Technical Report/研究博客 | 发现论文、机制与技术谱系；正式 Technical Report 可拆出多个独立机制 |
| 扎实度验证 | OpenReview 公开评审、Semantic Scholar 引用/作者图谱 | 验证实验、学术承接和人物轨迹 |
| 扩散验证 | Hugging Face Daily Papers、GitHub、Hacker News、Tavily、Reddit、X 标题搜索、Follow Builders | 验证讨论、实现、复现、KOL/播客二次传播；Tavily 只发现线索，作者本人发帖只核验身份 |

Product Hunt、普通 GitHub Trending 和产品热榜不再决定候选，只在未来需要观察技术落地时作为弱信号。

## 如何判断“新范式”

系统先区分作者写下的宏大问题与论文真正做出的 intervention。缺少清晰新机制、问题边界没有变化、外延空间过窄时直接淘汰；通过技术硬门槛后，再按发布证据决定是否占用本周报告篇幅：

- 已核验前沿组织发布的正式 Technical Report 优先进入解读，并逐项检查其中可独立承接的新机制。
- 普通论文即使来自强团队，也需要至少一条实质外部响应；身份仅仅“有机构署名”不等于有技术势能。
- 发布者背景一般或无法核验时，需要跨来源的有内容讨论、独立复现、产品承接或高关注研究者的二次解读。
- 作者本人在 X 或个人主页发布工作可用于核验身份、机构与既往轨迹，但不能把自己的宣传转化为“社区已经验证”。
- Tavily 免费搜索用于发现公开索引的 X、Reddit、小红书线索；它不是平台全量 API，也不参与声量计分。Reddit OAuth 只有在明确获批后才读取帖子、评论与互动指标。
- 前沿机构目录区分“主动抓取、已建立、监测、重点研究者”四层；完整名单与官方核验入口见 [前沿机构与研究者 Watchlist](docs/FRONTIER_RESEARCH_WATCHLIST.md)。

内部仍保留评分用于排序，但它不代替这些门槛，也不会出现在最终报告中。

## 每周交付物

报告文件位于 `reports/output/paradigm_radar_YYYY-MM-DD.md`。研究总编辑会先写约 500 字的本期 Memo，再按共同 background 把多篇工作组织成少数技术路线，连贯说明旧瓶颈、朴素思想、技术落地、路线分歧、应用价值和客观传播证据。正文必须用中文转述；论文名和必要术语可以保留英文，但英文摘要或长句会触发自动重写，重写仍不合格则任务失败且不发送邮件。

每条重要路线都会追踪第一作者或关键作者。人物档案优先核验当前机构、代表作、研究连续性、个人主页、ORCID、GitHub、LinkedIn 与公开邮箱；没有公开联系方式时，报告必须保留检索过的公开来源和能够确认的最低背景，不能只写“尚未验证”。

系统使用独立的 `database/paradigm_radar.db`。同一证据按 DOI、arXiv ID 或稳定 URL 去重；观察池和已报告路线会在每周重新检索近期讨论。同一范式只有在证据签名发生实质变化时才会以“进展更新”再次出现，因此相邻周不会原样重复。没有候选跨过联合门槛时会正常发送一份空雷达，而不是硬凑一条新范式。

## 配置与运行

环境要求为 Python 3.11+。安装依赖后复制配置模板：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

至少填写 DashScope Key 与 OpenAlex 免费 Key。Semantic Scholar Key 是可选增强项；没有学术邮箱时留空即可，人物轨迹仍会通过 OpenAlex、ORCID 与已核验个人主页补齐。

```bash
python main.py             # 立即执行一次
python main.py --schedule  # 每周五按配置持续运行
python main.py --report    # 不联网，重建最近报告并按配置发送邮件
python main.py --status    # 查看模型配置
```

定时参数：

```env
SOURCING_LOOKBACK_DAYS=7
SCHEDULE_DAY_OF_WEEK=fri
SCHEDULE_HOUR=9
SCHEDULE_MINUTE=0
SCHEDULE_TIMEZONE=Asia/Shanghai
```

如需观察最近一个月，将 `SOURCING_LOOKBACK_DAYS` 改为 `30`。跨周不重复由数据库证据签名保证，不依赖时间窗口是否重叠。

## 邮件推送

```env
EMAIL_PUSH_ENABLED=true
EMAIL_PUSH_REQUIRED=true
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USERNAME=your-email@qq.com
SMTP_PASSWORD=邮箱生成的SMTP授权码
SMTP_FROM=your-email@qq.com
SMTP_TO=recipient@example.com
SMTP_USE_SSL=true
SMTP_USE_STARTTLS=false
```

邮件主题会显示“新范式”和“进展更新”数量，完整 Markdown 作为附件发送。没有合格候选时也会发送空雷达，明确说明本周没有内容通过门槛。

手动执行 `python main.py` 与每周调度使用同一个流水线，都会在报告生成后发送邮件。开启
`EMAIL_PUSH_REQUIRED=true` 后，SMTP 失败会让任务明确失败，且不会登记为“已交付”。

## 云端自动运行

仓库已包含 GitHub Actions 工作流 `.github/workflows/weekly-radar.yml`：每周五
09:15（Asia/Shanghai）自动运行，也可以在 GitHub 的 Actions 页面手动运行。云端运行不依赖
本机开机，并会跨运行恢复去重数据库。完整上线步骤见
[`docs/CLOUD_AUTOMATION.md`](docs/CLOUD_AUTOMATION.md)。

## 主要代码

```text
paradigms/
  models.py          证据、范式、研究者模型
  discovery.py       论文/研究博客优先发现
  analyzer.py        新机制抽取与人物轨迹分析
  clustering.py      论文级结果聚合为范式
  enrichment.py      引用、实现、讨论、人物增强
  scoring.py         范式评分与增量优化硬门槛
sources/
  openalex_source.py
  openreview_source.py
  semantic_scholar_source.py
  researcher_profile_source.py
  paradigm_evidence_source.py
  social_web_search_source.py
  reddit_evidence_source.py
  priority_research_source.py
database/paradigm_store.py
reports/paradigm_generator.py
agents/paradigm_orchestrator.py
skills/weekly_research_memo/SKILL.md
skills/weekly_memo_revision/SKILL.md
```

旧版项目型流水线仍保留，可通过 `PIPELINE_MODE=legacy` 回退，但默认不再使用。
