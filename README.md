# AI 技术范式雷达

这个项目不再以 GitHub 项目或 Product Hunt 产品为基本单位，而是每周捕捉正在形成的 AI 技术范式，并从技术反向锁定关键研究者。

目标只有一个：在声量尚未完全爆发时，识别一个具备清晰机制、扎实证据和足够外延空间的新范式。纯 benchmark 提分、狭窄组件替换、微小效率优化不会因为热度高而进入报告。

## 新的工作流

```text
论文 / OpenReview / 研究博客
          ↓
   抽取“新机制假说”
          ↓
 新颖性 + 扎实度 + 范式外延硬门槛
          ↓
 GitHub / HF / HN / 引用网络验证扩散与复现
          ↓
 第一作者与关键作者的研究轨迹、现状、公开专业联系方式
          ↓
 每周技术范式雷达邮件
```

信源被分成三类，职责不能混用：

| 层级 | 信源 | 用途 |
|---|---|---|
| 原始发现 | arXiv、OpenAlex、OpenReview、官方研究博客 | 发现论文、机制与技术谱系 |
| 扎实度验证 | OpenReview 公开评审、Semantic Scholar 引用/作者图谱 | 验证实验、学术承接和人物轨迹 |
| 扩散验证 | Hugging Face Daily Papers、GitHub、Hacker News | 验证讨论、实现、复现和二次传播 |

Product Hunt、普通 GitHub Trending 和产品热榜不再决定候选，只在未来需要观察技术落地时作为弱信号。

## 如何判断“新范式”

每个候选按 100 分制计算：

| 维度 | 权重 | 判断内容 |
|---|---:|---|
| 新颖性 | 25 | 是否出现新的架构、数据、学习、推理或行动机制 |
| 技术扎实度 | 25 | 机制是否清楚，实验/对照/泛化/同行评议是否可信 |
| 范式外延 | 20 | 是否改变能力边界，能否扩张到多个任务和产品方向 |
| 扩散势能 | 15 | 是否出现跨平台讨论、独立实现、复现与加速传播 |
| 人物连续性 | 10 | 关键作者是否沿同一研究主线长期推进 |
| 绝对声量 | 5 | 仅作辅助证据，使用对数压缩，不能主导结果 |

增量优化惩罚最高可扣 35 分。缺少清晰新机制、问题边界没有变化、外延空间过窄时会触发硬拒绝，而不是降一点分后继续凑数。

## 每周交付物

报告文件位于 `reports/output/paradigm_radar_YYYY-MM-DD.md`，每个章节对应一个技术范式，包括：

- 技术谱系：上一代范式 → 当前关键节点
- 旧问题定义与新问题定义的差异
- 核心机制和为什么现在可行
- 论文、评审、引用、实现、讨论的跨平台证据表
- 扩散势能与当前仍缺失的证据
- 第一作者/关键作者的代表作、研究连续性和当前机构
- 已验证的公开专业主页、ORCID 或公开邮箱；没有则明确留空

系统使用独立的 `database/paradigm_radar.db`。同一证据按 DOI、arXiv ID 或稳定 URL 去重；同一范式只有在证据签名发生实质变化时才会以“进展更新”再次出现，因此相邻周不会原样重复。

## 配置与运行

环境要求为 Python 3.11+。安装依赖后复制配置模板：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

至少填写 DashScope Key。OpenAlex 免费 Key 和 Semantic Scholar Key 建议一并配置，否则学术覆盖和人物轨迹会变弱。

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
  paradigm_evidence_source.py
database/paradigm_store.py
reports/paradigm_generator.py
agents/paradigm_orchestrator.py
```

旧版项目型流水线仍保留，可通过 `PIPELINE_MODE=legacy` 回退，但默认不再使用。
