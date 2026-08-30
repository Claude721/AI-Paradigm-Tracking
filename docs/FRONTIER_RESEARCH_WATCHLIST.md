# AI 前沿机构与研究者 Watchlist

> 调研与核验日期：2026-08-15。机器可读的完整默认目录位于 `research_watchlist.py`；本文件解释为什么纳入、如何分层，以及哪些名字不能构成自动背书。

## 1. 名单不是“名气榜”

回看近几轮技术迁移，可以看到一条连续的纵轴：Scaling 与基础模型改变通用表示能力，RL 与搜索把模型推向 reasoning，工具使用与长程任务形成 Agent，视觉—语言—动作对齐催生 VLA/具身策略，视频自监督、生成模拟与空间表征又把 World Model 推向可预测、可规划的环境模型。真正需要持续观察的，是在这些节点上反复提出新问题、公开可审计机制，并且能吸引复现与后续工作的团队。

横向看，同一个时间截面存在四种不同对象：持续发布正式研究的大型实验室；论文数量庞大但主题过宽的综合机构；技术势能尚待验证的新模型/机器人公司；以及跨机构流动、但研究轨迹连续的关键研究者。把它们塞进一个白名单，会让“大机构的一篇小改动”和“关键团队的正式 Technical Report”获得同样待遇，正好制造项目最想避免的无用功。

因此默认目录采用四层：

1. **Priority pages** 只负责主动召回官方 Technical Report、论文索引和研究博客。
2. **Established organizations** 表示发布者身份与长期前沿产出可核验；普通论文仍要满足技术硬门槛与外部承接要求。
3. **Monitored organizations** 保证重要厂商与新型机器人团队被持续看到，但品牌本身不提供准入捷径。
4. **Priority researchers** 只提高人物轨迹核验优先级；必须完整姓名精确匹配，并同时取得公开主页、ORCID/OpenAlex 等身份依据。

## 2. 每周主动读取的官方入口

第一层是高信号研究索引。海外覆盖 [OpenAI Research](https://openai.com/research/index/)、[Anthropic Research](https://www.anthropic.com/research)、[Google DeepMind Research](https://deepmind.google/research/)、[Meta Research Publications](https://ai.meta.com/research/publications/)、[Microsoft AI Frontiers](https://www.microsoft.com/en-us/research/lab/ai-frontiers/publications/)、[NVIDIA GEAR](https://research.nvidia.com/labs/gear/publications/)、[Mistral Research](https://mistral.ai/news/?category=research)、[Cohere Research](https://cohere.com/research)、[Ai2](https://allenai.org/news)、[World Labs](https://www.worldlabs.ai/blog)、[Physical Intelligence](https://www.pi.website/research)、[Wayve Science](https://wayve.ai/science/)、[Toyota Research Institute Publications](https://www.tri.global/publications)、[Runway Research](https://runwayml.com/research/publications) 与 [Sakana Publications](https://pub.sakana.ai/)。

AI4S 作为独立技术栈覆盖 [FutureHouse Research](https://www.futurehouse.org/research)、[Arc Institute Newsroom](https://arcinstitute.org/news)、[Microsoft Research AI for Science](https://www.microsoft.com/en-us/research/lab/microsoft-research-ai-for-science/)、[Isomorphic Labs News](https://www.isomorphiclabs.com/news) 与 [Lila Sciences 技术页](https://www.lila.ai/tech)。Arc 与 Isomorphic 的旧聚合根路径在 2026-08 的 Actions 日志中持续返回 404，因此目录改用仍可枚举研究发布的官方 News 索引；入口迁移只修复召回，不改变组织分层。FutureHouse、Arc 与 Microsoft AI for Science 已有连续、可审计研究产出；Isomorphic 与 Lila 目前作为 `verified` 主动入口，不能仅凭公司叙事自动晋级。

中国侧覆盖 [Baidu ERNIE Blog](https://ernie.baidu.com/blog/)、[ByteDance Seed Research](https://seed.bytedance.com/en/research)、[Qwen Publications](https://qwenlm.github.io/publication/)、[Kimi Research](https://www.kimi.com/blog/)、[Moonshot AI](https://www.moonshot.ai/)、[Z.ai Blog](https://z.ai/blog)、[DeepSeek Transparency](https://www.deepseek.com/en/transparency/)、[DeepSeek API Updates](https://api-docs.deepseek.com/updates/)、[StepFun Research](https://chat.stepfun.com/research/en)、[MiniMax Blog](https://www.minimax.io/blog) 与 [ModelBest](https://modelbest.cn/)。其中 changelog 入口按最新显式日期切分发布事件；模型发布可以进入审计和机制初筛，但没有 Technical Report 或独立技术机制时不会因品牌直接进周报。

同一 owner 目录还维护一组已核验官方 GitHub 组织，用于发现“官方网页尚未收录、但新仓库已经发布”的事件。它不是第五种背书层：系统只按组织身份与仓库 `created_at` 召回，不靠项目名关键词；仓库链接到外部一手论文/官方技术页时使用外部原点，技术首先由结构化 README 与代码接口定义时才形成 `original_implementation` 假说。SDK、demo、聚合列表和信息不足的 README 不具备原点资格，star/fork 不能替代独立承接。

Google Research、Apple、Amazon、IBM、xAI、腾讯 ARC、华为诺亚、BAIR、Stanford CRFM、CMU RI 和 NYU CILVR 也会被主动读取，但默认 tier 是 `verified`：它们的页面要么覆盖面很宽，要么偏模型发布/动态页面，要么普通论文数量很大，不能仅凭入口直接晋级。Z.ai、Qwen 新站、腾讯 ARC 和华为诺亚的动态页面可能需要专用解析器；通用抓取失败时，arXiv/OpenAlex 仍是第二条召回路径。

## 3. 已建立的前沿组织

### 海外公司与独立研究组织

- 基础模型、reasoning 与 Agent：OpenAI、Anthropic、Google DeepMind、Google Research、Meta FAIR、Microsoft Research / AI Frontiers、NVIDIA Research、Apple ML Research、Amazon Science、IBM Research、xAI、Mistral AI、Cohere Labs、Allen Institute for AI。
- World Model、空间智能与 Physical AI：World Labs、Physical Intelligence、Wayve、Runway Research、Toyota Research Institute、NVIDIA GEAR / Spatial Intelligence、Sakana AI。
- AI4S 与闭环科学发现：FutureHouse、Arc Institute、Microsoft Research AI for Science；Isomorphic Labs 与 Lila Sciences 位于监测/verified 层，等待更多正式报告与独立验证。
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

名单围绕长期轨迹而不是单次热门论文组织。World Model / 空间智能关注 Yann LeCun、Mido Assran、Fei-Fei Li、Kaiming He、Jiajun Wu、David Ha、Danijar Hafner、Saining Xie、Rob Fergus；RL / reasoning 关注 Richard Sutton、David Silver、Demis Hassabis、Noam Brown；VLA / 机器人关注 Sergey Levine、Chelsea Finn、Pieter Abbeel、Ken Goldberg、Danfei Xu、Jitendra Malik、Trevor Darrell、Shuran Song、Yuke Zhu、Linxi Jim Fan、Dieter Fox、Russ Tedrake、Deepak Pathak、Marco Hutter；AI4S 关注 David Baker、John Jumper、Pushmeet Kohli、Christopher Bishop、Patrick Hsu 与 Brian Hie；基础模型与 Agent 关注 Ilya Sutskever、Oriol Vinyals、Jianfeng Gao、Ece Kamar、Percy Liang、Yejin Choi、Aidan Gomez、Joelle Pineau。

中国侧重点包括 Jun Zhu / 朱军、Yang Gao / 高阳、Zhiyuan Liu / 刘知远、Jie Tang / 唐杰、Song-Chun Zhu / 朱松纯、Zhilin Yang / 杨植麟、Daxin Jiang / 姜大昕、Cewu Lu / 卢策吾、He Wang / 王鹤、Hao Dong / 董豪、Hao Tang / 唐昊、Yizhou Wang / 王亦洲、Baoquan Chen / 陈宝权、Yao Mu / 穆尧、Guofeng Zhang / 章国锋、Hong Qiao / 乔红与 Yi Zeng / 曾毅。

三条身份关系是默认测试样例：

- [Fei-Fei Li 的 Stanford 页面](https://profiles.stanford.edu/fei-fei-li)与 [World Labs](https://www.worldlabs.ai/about)共同核验其学术与创业组织关系。
- [Kaiming He 的主页](https://people.csail.mit.edu/kaiming/)明确列出 MIT 与 Google DeepMind 的当前身份及其连续论文记录。
- [Yann LeCun 的 Meta 页面](https://ai.meta.com/people/yann-lecun/)和 [NYU 页面](https://cds.nyu.edu/team/yann-lecun/)共同核验 FAIR、NYU 与 JEPA 路线关系。

姓名命中不等于论文晋级。系统只有在人物档案已经取得公开 profile URL 或学术 ID 后，才写入“重点研究者身份已核验”；这个信号的 tier 仍是 `verified`，还需要技术硬门槛与外部承接。

T‑Rex 这类大型合作是人物规则的反例测试：报告应先识别 Dantong Niu、Zhuoyang Liu、Zekai Wang 的共同一作角色，再说明 Fei‑Fei Li、Ken Goldberg、Pieter Abbeel 等资深研究网络带来的研究连续性与传播势能，不能把工作简写成“李飞飞发布了一篇论文”。

## 6. 个人思想源与 KOL 目录

机器目录当前收录 37 个具名作者/编辑源，其中 19 个已经实测 RSS/Atom，可由周任务自动读取；其他条目只保存主页与已知 X handle，避免猜测 Feed 或依赖关键词搜索来确认身份。目录优先覆盖三种角色：反复提出新机制的研究者、把训练/推理过程讲清楚的技术实践者、以及能够提供高质量独立解读的编辑者。

自动读取的高信号个人 Feed 包括 [Lilian Weng](https://lilianweng.github.io/)、[Simon Willison](https://simonwillison.net/)、[Nathan Lambert](https://natolambert.com/writing)、[Sebastian Raschka](https://sebastianraschka.com/)、[Chip Huyen](https://huyenchip.com/)、[Eugene Yan](https://eugeneyan.com/)、[Hamel Husain](https://hamel.dev/)、[Jeremy Howard](https://jeremy.fast.ai/)、[Jay Alammar](https://jalammar.github.io/)、[Tim Dettmers](https://timdettmers.com/)、[Chris Olah](https://colah.github.io/)、[Danijar Hafner](https://danijar.com/)、[George Hotz](https://geohot.github.io/blog/) 与 [苏剑林](https://kexue.fm/)。目录对作者角色做保守区分：Lilian Weng、Simon Willison、Nathan Lambert、Sebastian Raschka 与 Jay Alammar 的 Feed 默认先作为高质量解释/扩散证据；Chip Huyen、Eugene Yan、Hamel Husain、Jeremy Howard、Tim Dettmers、Chris Olah、Danijar Hafner、George Hotz 与苏剑林的机制性文章可以提出 `concept_essay` 假说。任何作者名气和自发传播都不能替代可执行步骤、可证伪边界与外部承接。

自动读取但默认只作二次解读的源包括 [Jack Clark / Import AI](https://jack-clark.net/)、[Gary Marcus](https://garymarcus.substack.com/)、[Zvi Mowshowitz](https://thezvi.substack.com/)、[Astral Codex Ten](https://www.astralcodexten.com/) 与 [Latent Space](https://www.latent.space/)。它们可帮助理解社区在讨论什么，不能仅凭一篇评论生成新路线。

无稳定 Feed 的重点主页包括 [Andrej Karpathy](https://karpathy.ai/)、[Yann LeCun](https://yann.lecun.com/)、[Fei‑Fei Li](https://profiles.stanford.edu/fei-fei-li)、[Kaiming He](https://kaiminghe.com/)、[Yoshua Bengio](https://yoshuabengio.org/)、[Richard Sutton](http://incompleteideas.net/)、[Sergey Levine](https://people.eecs.berkeley.edu/~svlevine/)、[Chelsea Finn](https://ai.stanford.edu/~cbfinn/)、[Linxi Jim Fan](https://www.jimfan.me/)、[Percy Liang](https://cs.stanford.edu/~pliang/)、[Andrew Ng](https://www.andrewng.org/)、[François Chollet](https://fchollet.com/)、[David Ha](https://ha-david.github.io/)、[Jun Zhu](http://ml.cs.tsinghua.edu.cn/~jun/)、[Zhiyuan Liu](https://nlp.csai.tsinghua.edu.cn/~lzy/)、[Jie Tang](https://keg.cs.tsinghua.edu.cn/jietang/)、[Zhi‑Hua Zhou](https://cs.nju.edu.cn/zhouzh/) 与 [Weinan E](https://weinan-e.com/)。配置 X API 后，目录中的 handle 可用于精确账号追踪；没有 X API 时这些主页仍参与身份核验，但系统不会声称已覆盖其全部即时发言。

LessWrong 与 Alignment Forum 单独作为社区思想源：使用官方 RSS 的 curated/frontpage/karmaThreshold 能避开脆弱的页面爬虫，但阈值只表示 Feed 选择条件。原帖首先是一个机制原点；只有其他主体的实质评论、复现、引用或采用才属于二次承接。

## 7. 配置与维护规则

- 默认使用 `RESEARCH_WATCHLIST_MODE=merge`。GitHub Variables 中的值只追加，不会冻结未来代码更新；只有明确需要完全自定义时才用 `replace`。
- 不添加整所大学、宽泛企业母体或歧义短词；禁止裸称包括 `AI Lab`、`ARC Lab`、`Seed`、`GLM`、`Ling`。
- 组织别名按完整字段/分段精确匹配，不再双向子串匹配。短别名 `FAIR`、`1X` 只有字段完整等于该别名时才成立。
- 用户追加的 Priority 页面没有内置 owner 元数据，只允许同域抓取并视为 `verified`；不能通过网页 `og:site_name` 冒充知名机构。
- 每季度核验页面可访问性、团队更名、研究者当前任职与研究方向；重大模型厂商发布正式 Technical Report 时即时更新。
- 新增/升级组织时至少记录一个官方入口、标准名称、必要别名、明确的研究方向与分层理由。名单只影响召回和身份先验，永远不覆盖项目的技术硬门槛。
- 新增个人思想源时优先验证 RSS/Atom；没有稳定 Feed 时只登记主页/X，不猜地址。必须明确 `concept_origin` 或 `secondary_only`，前者也只能生成机制假说，不能自动晋级。
