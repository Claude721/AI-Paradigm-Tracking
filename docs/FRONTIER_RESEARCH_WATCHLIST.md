# AI 前沿机构与研究者 Watchlist

> 调研与核验日期：2026-07-19。机器可读的完整默认目录位于 `research_watchlist.py`；本文件解释为什么纳入、如何分层，以及哪些名字不能构成自动背书。

## 1. 名单不是“名气榜”

回看近几轮技术迁移，可以看到一条连续的纵轴：Scaling 与基础模型改变通用表示能力，RL 与搜索把模型推向 reasoning，工具使用与长程任务形成 Agent，视觉—语言—动作对齐催生 VLA/具身策略，视频自监督、生成模拟与空间表征又把 World Model 推向可预测、可规划的环境模型。真正需要持续观察的，是在这些节点上反复提出新问题、公开可审计机制，并且能吸引复现与后续工作的团队。

横向看，同一个时间截面存在四种不同对象：持续发布正式研究的大型实验室；论文数量庞大但主题过宽的综合机构；技术势能尚待验证的新模型/机器人公司；以及跨机构流动、但研究轨迹连续的关键研究者。把它们塞进一个白名单，会让“大机构的一篇小改动”和“关键团队的正式 Technical Report”获得同样待遇，正好制造项目最想避免的无用功。

因此默认目录采用四层：

1. **Priority pages** 只负责主动召回官方 Technical Report、论文索引和研究博客。
2. **Established organizations** 表示发布者身份与长期前沿产出可核验；普通论文仍要满足技术硬门槛与外部承接要求。
3. **Monitored organizations** 保证重要厂商与新型机器人团队被持续看到，但品牌本身不提供准入捷径。
4. **Priority researchers** 只提高人物轨迹核验优先级；必须完整姓名精确匹配，并同时取得公开主页、ORCID/OpenAlex 等身份依据。

## 2. 每周主动读取的官方入口

第一层是高信号研究索引。海外覆盖 [OpenAI Research](https://openai.com/research/index/)、[Anthropic Research](https://www.anthropic.com/research)、[Google DeepMind Research](https://deepmind.google/research/)、[Meta Research Publications](https://ai.meta.com/research/publications/)、[Microsoft AI Frontiers](https://www.microsoft.com/en-us/research/lab/ai-frontiers/publications/)、[NVIDIA GEAR](https://research.nvidia.com/labs/gear/publications/)、[Mistral Research](https://mistral.ai/news/?category=research)、[Cohere Research](https://cohere.com/research)、[Ai2](https://allenai.org/news)、[World Labs](https://www.worldlabs.ai/blog)、[Physical Intelligence](https://www.pi.website/research)、[Runway Research](https://runwayml.com/research/publications) 与 [Sakana Publications](https://pub.sakana.ai/)。

中国侧覆盖 [Baidu ERNIE Blog](https://ernie.baidu.com/blog/)、[ByteDance Seed Research](https://seed.bytedance.com/en/research)、[Qwen Publications](https://qwenlm.github.io/publication/)、[Moonshot AI](https://www.moonshot.ai/)、[Z.ai Blog](https://z.ai/blog)、[DeepSeek Transparency](https://www.deepseek.com/en/transparency/)、[StepFun Research](https://chat.stepfun.com/research/en)、[MiniMax Blog](https://www.minimax.io/blog) 与 [ModelBest](https://modelbest.cn/)。

Google Research、Apple、Amazon、IBM、xAI、腾讯 ARC、华为诺亚、BAIR、Stanford CRFM、CMU RI 和 NYU CILVR 也会被主动读取，但默认 tier 是 `verified`：它们的页面要么覆盖面很宽，要么偏模型发布/动态页面，要么普通论文数量很大，不能仅凭入口直接晋级。Z.ai、Qwen 新站、腾讯 ARC 和华为诺亚的动态页面可能需要专用解析器；通用抓取失败时，arXiv/OpenAlex 仍是第二条召回路径。

## 3. 已建立的前沿组织

### 海外公司与独立研究组织

- 基础模型、reasoning 与 Agent：OpenAI、Anthropic、Google DeepMind、Google Research、Meta FAIR、Microsoft Research / AI Frontiers、NVIDIA Research、Apple ML Research、Amazon Science、IBM Research、xAI、Mistral AI、Cohere Labs、Allen Institute for AI。
- World Model、空间智能与 Physical AI：World Labs、Physical Intelligence、Wayve、Runway Research、Toyota Research Institute、NVIDIA GEAR / Spatial Intelligence、Sakana AI。
- Hugging Face 与其研究团队保留在目录中用于身份归一和开源承接，但 Hugging Face 平台热度不能单独生成范式。

World Model 路线尤其说明了为什么需要同时维护“机构—实验室—人物”关系：[World Labs 官方介绍](https://www.worldlabs.ai/about)确认 Fei-Fei Li 等创始团队聚焦空间智能；[Meta V-JEPA](https://ai.meta.com/research/vjepa/)把 JEPA 延伸到视频预测；[NVIDIA Cosmos](https://research.nvidia.com/labs/dir/cosmos1/)与 GEAR 则连接世界基础模型和机器人训练。它们共享问题背景，但解法、数据与下游承接不同，报告应按路线综合，而不是按品牌逐条列举。

### 中国公司与独立研究组织

- 大厂前沿研究：Baidu Research / ERNIE / PaddlePaddle、Tencent Hunyuan / AI Lab / ARC、ByteDance Seed、Alibaba Qwen / DAMO、Huawei Noah / PanGu。
- 基础模型公司：Moonshot AI / 月之暗面 / Kimi Team、DeepSeek、Zhipu AI / 智谱 / Z.ai / GLM Team、MiniMax、StepFun、ModelBest / 面壁智能 / OpenBMB / MiniCPM。
- 高势能公共/独立研究组织：Shanghai AI Laboratory / InternLM / OpenGVLab、BAAI / FlagOpen、BIGAI、Peng Cheng Laboratory、Shanghai Qi Zhi Institute、SenseTime Research、CASIA 多模态人工智能系统实验室。

这里使用研究发布方而不是产品俗称：文心一言归一到 ERNIE Team / Baidu Research，Kimi 归一到 Moonshot AI / Kimi Team，GLM 与 Z.AI 归一到 Zhipu AI，通义千问归一到 Qwen Team / Alibaba DAMO。产品名可以辅助发现，不能脱离官方域名或论文 affiliation 单独证明发布者身份。

### 具名高校实验室

只纳入可辨认的实验室，不纳入整所学校：

- 海外：Berkeley BAIR / Robot Learning Lab，Stanford SAIL / SVL / CRFM / IRIS，MIT CSAIL / Improbable AI，CMU Robotics Institute / REAL / Pathak Group，NYU CILVR，Mila，Princeton AI / PLI，Kempner Institute，Oxford VGG，Alberta RLAI，ETH Robotic Systems Lab，UCL Gatsby 与 Max Planck Institute for Intelligent Systems。
- 中国：Tsinghua TSAIL / THUNLP / KEG / EVAR / EIR / THBI，PKU CFCS / Center for Embodied Intelligence / EPIC / PKU-Agibot，SJTU MVIG，ZJU CAD&CG，CUHK MMLab 与 HKU MMLab。

[Stanford 研究组目录](https://ai.stanford.edu/research-groups/)能核验 SAIL/SVL 等具体团队；[Kaiming He 个人主页](https://people.csail.mit.edu/kaiming/)能核验其 MIT CSAIL 与 Google DeepMind 身份；[NYU CILVR 论文索引](https://wp.nyu.edu/cilvr/cilvr-group-publications/)则直接呈现 JEPA/V-JEPA 的连续研究线。中国侧以具体实验室为界，例如 [Jun Zhu / TSAIL](https://ml.cs.tsinghua.edu.cn/~jun/research.shtml)、[Yang Gao / 清华 IIIS](https://iiis.tsinghua.edu.cn/rydw1/qzjs/gaoyang.htm) 和 [PKU 具身智能与机器人中心](https://www.ai.pku.edu.cn/en/Centers/Centers_for_Artificial_General_Intelligence/Center_for_Embodied_Intelligence_and_Robotics.htm)。

## 4. 监测层：覆盖，但不自动背书

以下组织有产品势能、资金/人才密度或近期技术活动，值得持续观察；但公开 Technical Report 的连续性、研究开放程度或独立承接仍不足以让品牌本身成为准入依据：

- 海外：Thinking Machines Lab、Skild AI、Figure AI、1X Technologies、Tesla AI。
- 中国基础模型/平台：Baichuan AI、01.AI、Xiaomi MiMo、Meituan LongCat、Ant Group AI / InclusionAI、JD Explore / JoyAI、360 AI Research、iFLYTEK、Skywork、vivo AI / BlueLM、OPPO AI / AndesGPT、NetEase Fuxi。
- 视频与机器人：Kuaishou Kling、ShengShu / Vidu、Horizon Robotics、AgiBot、Galbot、Unitree Robotics。

这些组织只有在出现**正式技术报告、可复核实验、独立实现/复现或实质二次讨论**时才升级。演示视频、模型发布页、融资新闻和作者自我宣传都不能替代技术证据。

## 5. 重点研究者图谱

名单围绕长期轨迹而不是单次热门论文组织。World Model / 空间智能关注 Yann LeCun、Mido Assran、Fei-Fei Li、Kaiming He、Jiajun Wu、David Ha、Danijar Hafner、Saining Xie、Rob Fergus；RL / reasoning 关注 Richard Sutton、David Silver、Demis Hassabis、Noam Brown；VLA / 机器人关注 Sergey Levine、Chelsea Finn、Pieter Abbeel、Jitendra Malik、Trevor Darrell、Shuran Song、Yuke Zhu、Linxi Jim Fan、Dieter Fox、Russ Tedrake、Deepak Pathak、Marco Hutter；基础模型与 Agent 关注 Ilya Sutskever、Oriol Vinyals、Jianfeng Gao、Ece Kamar、Percy Liang、Yejin Choi、Aidan Gomez、Joelle Pineau。

中国侧重点包括 Jun Zhu / 朱军、Yang Gao / 高阳、Zhiyuan Liu / 刘知远、Jie Tang / 唐杰、Song-Chun Zhu / 朱松纯、Zhilin Yang / 杨植麟、Daxin Jiang / 姜大昕、Cewu Lu / 卢策吾、He Wang / 王鹤、Hao Dong / 董豪、Hao Tang / 唐昊、Yizhou Wang / 王亦洲、Baoquan Chen / 陈宝权、Yao Mu / 穆尧、Guofeng Zhang / 章国锋、Hong Qiao / 乔红与 Yi Zeng / 曾毅。

三条身份关系是默认测试样例：

- [Fei-Fei Li 的 Stanford 页面](https://profiles.stanford.edu/fei-fei-li)与 [World Labs](https://www.worldlabs.ai/about)共同核验其学术与创业组织关系。
- [Kaiming He 的主页](https://people.csail.mit.edu/kaiming/)明确列出 MIT 与 Google DeepMind 的当前身份及其连续论文记录。
- [Yann LeCun 的 Meta 页面](https://ai.meta.com/people/yann-lecun/)和 [NYU 页面](https://cds.nyu.edu/team/yann-lecun/)共同核验 FAIR、NYU 与 JEPA 路线关系。

姓名命中不等于论文晋级。系统只有在人物档案已经取得公开 profile URL 或学术 ID 后，才写入“重点研究者身份已核验”；这个信号的 tier 仍是 `verified`，还需要技术硬门槛与外部承接。

## 6. 配置与维护规则

- 默认使用 `RESEARCH_WATCHLIST_MODE=merge`。GitHub Variables 中的值只追加，不会冻结未来代码更新；只有明确需要完全自定义时才用 `replace`。
- 不添加整所大学、宽泛企业母体或歧义短词；禁止裸称包括 `AI Lab`、`ARC Lab`、`Seed`、`GLM`、`Ling`。
- 组织别名按完整字段/分段精确匹配，不再双向子串匹配。短别名 `FAIR`、`1X` 只有字段完整等于该别名时才成立。
- 用户追加的 Priority 页面没有内置 owner 元数据，只允许同域抓取并视为 `verified`；不能通过网页 `og:site_name` 冒充知名机构。
- 每季度核验页面可访问性、团队更名、研究者当前任职与研究方向；重大模型厂商发布正式 Technical Report 时即时更新。
- 新增/升级组织时至少记录一个官方入口、标准名称、必要别名、明确的研究方向与分层理由。名单只影响召回和身份先验，永远不覆盖项目的技术硬门槛。
