"""版本化的 AI 前沿机构、实验室、研究者与官方发布入口。

名单的作用是提高召回与核验优先级，不替代技术硬门槛。大学只登记具体
实验室，不登记整所学校；研究者只有在公开身份可核验后才贡献势能。
"""

from __future__ import annotations


ORGANIZATIONS: tuple[dict, ...] = (
    # ── 海外基础模型、Agent 与安全研究 ──────────────────────
    {"id": "openai", "name": "OpenAI", "aliases": ("OpenAI",), "kind": "company", "focus": "LLM、reasoning、Agent 与多模态"},
    {"id": "anthropic", "name": "Anthropic", "aliases": ("Anthropic",), "kind": "company", "focus": "前沿模型、对齐、可解释性与 Agent"},
    {"id": "google-deepmind", "name": "Google DeepMind", "aliases": ("Google DeepMind", "DeepMind", "Google Brain"), "kind": "company_lab", "focus": "RL、Gemini、机器人与 World Model"},
    {"id": "google-research", "name": "Google Research", "aliases": ("Google Research", "Google AI Research"), "kind": "company_lab", "focus": "基础模型、视觉、语言、系统与 AI for Science"},
    {"id": "meta-fair", "name": "Meta FAIR", "aliases": ("Meta FAIR", "Meta AI", "Facebook AI Research", "FAIR"), "kind": "company_lab", "focus": "开放模型、自监督、JEPA 与 World Model"},
    {"id": "microsoft-research", "name": "Microsoft Research", "aliases": ("Microsoft Research", "Microsoft Research Asia", "MSRA", "Microsoft AI"), "kind": "company_lab", "focus": "基础模型、Agent、系统与多模态"},
    {"id": "nvidia-research", "name": "NVIDIA Research", "aliases": ("NVIDIA Research", "NVIDIA AI", "NVIDIA GEAR", "NVIDIA Seattle Robotics Lab", "NVIDIA Spatial Intelligence Lab"), "kind": "company_lab", "focus": "Physical AI、VLA、机器人与 World Foundation Model"},
    {"id": "apple-ml", "name": "Apple Machine Learning Research", "aliases": ("Apple Machine Learning Research", "Apple AI/ML", "Apple Machine Learning"), "kind": "company_lab", "focus": "端侧模型、架构、多模态与 Agent"},
    {"id": "amazon-science", "name": "Amazon Science", "aliases": ("Amazon Science", "Amazon AGI", "Amazon AI Labs", "AWS AI Labs"), "kind": "company_lab", "focus": "基础模型、Agent、机器人与系统"},
    {"id": "ibm-research", "name": "IBM Research", "aliases": ("IBM Research",), "kind": "company_lab", "focus": "基础模型、AI 系统与科学智能"},
    {"id": "xai", "name": "xAI", "aliases": ("xAI",), "kind": "company", "focus": "前沿基础模型与推理"},
    {"id": "mistral", "name": "Mistral AI", "aliases": ("Mistral AI",), "kind": "company", "focus": "高效开放模型、MoE 与 Agent"},
    {"id": "cohere-labs", "name": "Cohere Labs", "aliases": ("Cohere Labs", "Cohere For AI", "Cohere"), "kind": "company_lab", "focus": "多语言、检索、开放模型与安全"},
    {"id": "ai2", "name": "Allen Institute for AI", "aliases": ("Allen Institute for AI", "Ai2", "AI2"), "kind": "independent_lab", "focus": "开放基础模型、数据、Agent 与机器人"},
    {"id": "huggingface-research", "name": "Hugging Face Research", "aliases": ("Hugging Face Research", "Hugging Face"), "kind": "company_lab", "focus": "开放模型、数据与高效训练"},
    {"id": "thinking-machines", "name": "Thinking Machines Lab", "aliases": ("Thinking Machines Lab",), "kind": "company_lab", "focus": "前沿模型、训练与人机协作"},
    # ── 海外空间智能、机器人与 World Model ───────────────────
    {"id": "world-labs", "name": "World Labs", "aliases": ("World Labs",), "kind": "company_lab", "focus": "空间智能与 3D World Model"},
    {"id": "physical-intelligence", "name": "Physical Intelligence", "aliases": ("Physical Intelligence", "π0 Team", "PI Robotics"), "kind": "company_lab", "focus": "通用机器人基础模型与 VLA"},
    {"id": "wayve", "name": "Wayve", "aliases": ("Wayve", "Wayve AI"), "kind": "company_lab", "focus": "具身驾驶与生成式 World Model"},
    {"id": "runway", "name": "Runway Research", "aliases": ("Runway Research", "Runway AI", "Runway"), "kind": "company_lab", "focus": "视频生成与世界模拟"},
    {"id": "sakana", "name": "Sakana AI", "aliases": ("Sakana AI",), "kind": "company_lab", "focus": "新架构、模型合并与自动科研"},
    {"id": "toyota-ri", "name": "Toyota Research Institute", "aliases": ("Toyota Research Institute", "TRI"), "kind": "company_lab", "focus": "机器人学习、驾驶与人机协作"},
    {"id": "skild-ai", "name": "Skild AI", "aliases": ("Skild AI",), "kind": "company_lab", "focus": "通用机器人基础模型"},
    {"id": "figure-ai", "name": "Figure AI", "aliases": ("Figure AI",), "kind": "company_lab", "focus": "人形机器人与端到端具身模型"},
    {"id": "one-x", "name": "1X Technologies", "aliases": ("1X Technologies", "1X"), "kind": "company_lab", "focus": "人形机器人与世界模型"},
    {"id": "tesla-ai", "name": "Tesla AI", "aliases": ("Tesla AI", "Tesla Autopilot"), "kind": "company_lab", "focus": "具身驾驶、人形机器人与端到端学习"},
    # ── AI for Science、自动实验与科学基础模型 ───────────────
    {"id": "isomorphic-labs", "name": "Isomorphic Labs", "aliases": ("Isomorphic Labs",), "kind": "company_lab", "focus": "蛋白结构、药物设计与科学基础模型"},
    {"id": "futurehouse", "name": "FutureHouse", "aliases": ("FutureHouse",), "kind": "independent_lab", "focus": "科学 Agent、自动假说与端到端生物发现"},
    {"id": "arc-institute", "name": "Arc Institute", "aliases": ("Arc Institute",), "kind": "independent_lab", "focus": "生物基础模型、基因组设计与实验验证"},
    {"id": "lila-sciences", "name": "Lila Sciences", "aliases": ("Lila Sciences", "LILA"), "kind": "company_lab", "focus": "科学推理模型、自动实验室与闭环学习"},
    {"id": "microsoft-ai4science", "name": "Microsoft Research AI for Science", "aliases": ("Microsoft Research AI for Science", "Microsoft Research AI4Science", "Microsoft AI for Science"), "kind": "company_lab", "focus": "科学基础模型、分子材料、天气与科学计算"},
    # ── 中国基础模型与前沿研究组织 ───────────────────────────
    {"id": "baidu", "name": "Baidu Research", "aliases": ("Baidu Research", "Baidu AI", "百度研究院", "百度文心", "ERNIE Team", "PaddlePaddle"), "kind": "company_lab", "focus": "文心、飞桨、知识增强与多模态"},
    {"id": "tencent-hunyuan", "name": "Tencent Hunyuan", "aliases": ("Tencent Hunyuan", "腾讯混元", "Hunyuan Team", "Tencent AI Lab", "Tencent ARC Lab"), "kind": "company_lab", "focus": "混元基础模型、多模态与 World Model"},
    {"id": "bytedance-seed", "name": "ByteDance Seed", "aliases": ("ByteDance Seed", "Bytedance Seed", "字节跳动 Seed", "字节 Seed", "豆包大模型团队"), "kind": "company_lab", "focus": "LLM、Agent、视频、World Model 与机器人"},
    {"id": "alibaba-qwen", "name": "Alibaba Qwen", "aliases": ("Alibaba Qwen", "Qwen Team", "通义千问团队", "阿里云通义", "Alibaba DAMO Academy", "阿里达摩院"), "kind": "company_lab", "focus": "千问基础模型、多模态、Agent 与开源生态"},
    {"id": "moonshot", "name": "Moonshot AI", "aliases": ("Moonshot AI", "月之暗面", "Kimi Team", "Kimi 智能助手"), "kind": "company", "focus": "长上下文、Agent、推理与高效架构"},
    {"id": "zhipu", "name": "Zhipu AI / Z.ai", "aliases": ("Zhipu AI", "ZhipuAI", "智谱 AI", "智谱华章", "Z.ai", "GLM Team", "ChatGLM"), "kind": "company", "focus": "GLM、Agent、推理与多模态"},
    {"id": "deepseek", "name": "DeepSeek AI", "aliases": ("DeepSeek AI", "DeepSeek-AI", "DeepSeek", "深度求索"), "kind": "company_lab", "focus": "MoE、MLA、reasoning 与训练系统"},
    {"id": "modelbest", "name": "ModelBest / OpenBMB", "aliases": ("ModelBest", "面壁智能", "OpenBMB", "MiniCPM Team", "MiniCPM"), "kind": "company_lab", "focus": "端侧高效模型、全模态与 Agent"},
    {"id": "stepfun", "name": "StepFun", "aliases": ("StepFun", "阶跃星辰", "阶跃 AI"), "kind": "company", "focus": "基础模型、多模态、Agent 与 World Model"},
    {"id": "minimax", "name": "MiniMax", "aliases": ("MiniMax", "稀宇科技", "abab Team", "海螺 AI"), "kind": "company", "focus": "MoE、Agent、语音与视频生成"},
    {"id": "baichuan", "name": "Baichuan AI", "aliases": ("Baichuan AI", "百川智能", "Baichuan Team"), "kind": "company", "focus": "基础模型、医疗与 Agent"},
    {"id": "01ai", "name": "01.AI", "aliases": ("01.AI", "零一万物", "Yi Model Team"), "kind": "company", "focus": "Yi 基础模型与 Agent"},
    {"id": "xiaomi-mimo", "name": "Xiaomi MiMo", "aliases": ("Xiaomi MiMo", "MiMo Team", "小米大模型团队", "Xiaomi AI Lab"), "kind": "company_lab", "focus": "推理、Agent 与端侧基础模型"},
    {"id": "meituan-longcat", "name": "Meituan LongCat", "aliases": ("Meituan LongCat", "LongCat Team", "美团 LongCat", "美团大模型团队"), "kind": "company_lab", "focus": "高效 MoE、Agent 与产业模型"},
    {"id": "ant-bailing", "name": "Ant Group AI", "aliases": ("Ant Group AI", "蚂蚁集团百灵", "蚂蚁百灵", "InclusionAI"), "kind": "company_lab", "focus": "基础模型、多模态、Agent 与具身智能"},
    {"id": "jd-explore", "name": "JD Explore Academy", "aliases": ("JD Explore Academy", "京东探索研究院", "京东言犀", "JoyAI"), "kind": "company_lab", "focus": "产业基础模型、多模态与 Agent"},
    {"id": "360-ai", "name": "360 AI Research", "aliases": ("360 AI Research", "360 智脑团队", "三六零人工智能研究院"), "kind": "company_lab", "focus": "基础模型、搜索与 Agent"},
    {"id": "iflytek", "name": "iFLYTEK Research", "aliases": ("iFLYTEK Research", "科大讯飞研究院", "讯飞星火团队", "认知智能全国重点实验室"), "kind": "company_lab", "focus": "语音、语言模型与认知智能"},
    {"id": "kuaishou-kling", "name": "Kuaishou Kling AI", "aliases": ("Kuaishou Kling AI", "快手可灵团队", "Kling AI Team"), "kind": "company_lab", "focus": "视频生成与世界模拟"},
    {"id": "shengshu-vidu", "name": "ShengShu AI / Vidu", "aliases": ("ShengShu AI", "生数科技", "Vidu Team"), "kind": "company_lab", "focus": "视频生成与 World Model"},
    {"id": "skywork", "name": "Skywork AI", "aliases": ("Skywork AI", "昆仑万维天工团队", "天工大模型团队"), "kind": "company_lab", "focus": "基础模型、多模态与 Agent"},
    {"id": "netease-fuxi", "name": "NetEase Fuxi Lab", "aliases": ("NetEase Fuxi Lab", "网易伏羲实验室"), "kind": "company_lab", "focus": "游戏智能、Agent 与具身智能"},
    {"id": "vivo-ai", "name": "vivo AI Lab", "aliases": ("vivo AI Lab", "BlueLM Team"), "kind": "company_lab", "focus": "端侧基础模型与多模态"},
    {"id": "oppo-ai", "name": "OPPO AI Center", "aliases": ("OPPO AI Center", "AndesGPT Team", "OPPO AI Lab"), "kind": "company_lab", "focus": "端侧基础模型、多模态与 Agent"},
    {"id": "horizon-robotics", "name": "Horizon Robotics", "aliases": ("Horizon Robotics", "地平线机器人"), "kind": "company_lab", "focus": "具身驾驶与端侧智能"},
    {"id": "agibot", "name": "AgiBot", "aliases": ("AgiBot", "智元机器人"), "kind": "company_lab", "focus": "人形机器人、VLA 与具身数据"},
    {"id": "galbot", "name": "Galbot", "aliases": ("Galbot", "银河通用"), "kind": "company_lab", "focus": "通用机器人与具身基础模型"},
    {"id": "unitree", "name": "Unitree Robotics", "aliases": ("Unitree Robotics", "宇树科技"), "kind": "company_lab", "focus": "机器人本体、运动控制与具身智能"},
    {"id": "sensetime", "name": "SenseTime Research", "aliases": ("SenseTime Research", "SenseTime", "商汤研究院", "商汤科技", "SenseNova"), "kind": "company_lab", "focus": "多模态、视觉、日日新与具身智能"},
    {"id": "huawei-noah", "name": "Huawei Noah's Ark Lab", "aliases": ("Huawei Noah's Ark Lab", "Noah's Ark Lab", "华为诺亚方舟实验室", "Huawei PanGu", "盘古大模型团队"), "kind": "company_lab", "focus": "模型架构、优化、端侧与物理智能"},
    {"id": "shanghai-ai-lab", "name": "Shanghai AI Laboratory", "aliases": ("Shanghai AI Laboratory", "Shanghai AI Lab", "上海人工智能实验室", "InternLM Team", "OpenGVLab"), "kind": "independent_lab", "focus": "书生、InternVL、科学智能与具身智能"},
    {"id": "baai", "name": "Beijing Academy of Artificial Intelligence", "aliases": ("Beijing Academy of Artificial Intelligence", "BAAI", "北京智源人工智能研究院", "智源研究院"), "kind": "independent_lab", "focus": "开放基础模型、多模态、具身与脑科学"},
    {"id": "bigai", "name": "Beijing Institute for General Artificial Intelligence", "aliases": ("Beijing Institute for General Artificial Intelligence", "BIGAI", "北京通用人工智能研究院", "通研院"), "kind": "independent_lab", "focus": "认知架构、World Model 与通用智能体"},
    {"id": "pengcheng-lab", "name": "Peng Cheng Laboratory", "aliases": ("Peng Cheng Laboratory", "PCL", "鹏城实验室"), "kind": "independent_lab", "focus": "鹏城大模型、算力系统与开放生态"},
    {"id": "shanghai-qizhi", "name": "Shanghai Qi Zhi Institute", "aliases": ("Shanghai Qi Zhi Institute", "Shanghai Qizhi Institute", "上海期智研究院"), "kind": "independent_lab", "focus": "机器学习、机器人、AI 系统与理论"},
    # ── 海内外具体大学实验室；不使用整校名称 ─────────────────
    {"id": "bair", "name": "Berkeley Artificial Intelligence Research", "aliases": ("Berkeley Artificial Intelligence Research", "BAIR"), "kind": "university_lab", "focus": "机器人学习、视觉、RL 与基础模型"},
    {"id": "stanford-sail", "name": "Stanford Artificial Intelligence Laboratory", "aliases": ("Stanford Artificial Intelligence Laboratory", "Stanford AI Lab", "SAIL"), "kind": "university_lab", "focus": "基础模型、Agent、视觉与机器人"},
    {"id": "stanford-svl", "name": "Stanford Vision and Learning Lab", "aliases": ("Stanford Vision and Learning Lab", "Stanford Vision Lab", "SVL"), "kind": "university_lab", "focus": "空间智能、视觉与机器人"},
    {"id": "mit-csail", "name": "MIT CSAIL", "aliases": ("MIT CSAIL", "Computer Science and Artificial Intelligence Laboratory"), "kind": "university_lab", "focus": "基础模型、视觉、机器人与系统"},
    {"id": "cmu-ri", "name": "CMU Robotics Institute", "aliases": ("CMU Robotics Institute", "Carnegie Mellon Robotics Institute"), "kind": "university_lab", "focus": "机器人学习、具身智能与自主系统"},
    {"id": "cmu-real", "name": "CMU REAL", "aliases": ("CMU REAL", "Robotics, Embodied AI, and Learning"), "kind": "university_lab", "focus": "机器人、具身 AI 与学习"},
    {"id": "stanford-crfm", "name": "Stanford CRFM", "aliases": ("Stanford CRFM", "Center for Research on Foundation Models"), "kind": "university_lab", "focus": "基础模型、评测与开放生态"},
    {"id": "stanford-iris", "name": "Stanford IRIS Lab", "aliases": ("Stanford IRIS Lab", "Interactive Robotics and Intelligent Systems Lab"), "kind": "university_lab", "focus": "机器人学习与人机协作"},
    {"id": "berkeley-rll", "name": "Berkeley Robot Learning Lab", "aliases": ("Berkeley Robot Learning Lab", "Berkeley RLL"), "kind": "university_lab", "focus": "机器人学习、RL 与通用策略"},
    {"id": "mit-improbable", "name": "MIT Improbable AI Lab", "aliases": ("MIT Improbable AI Lab", "Improbable AI Lab"), "kind": "university_lab", "focus": "机器人学习、灵巧操作与具身智能"},
    {"id": "cmu-pathak", "name": "Pathak Research Group", "aliases": ("Pathak Research Group", "CMU Pathak Lab"), "kind": "university_lab", "focus": "自监督、机器人学习与 World Model"},
    {"id": "nyu-cilvr", "name": "NYU CILVR", "aliases": ("NYU CILVR", "CILVR Lab", "Computational Intelligence, Learning, Vision, and Robotics"), "kind": "university_lab", "focus": "JEPA、World Model、自监督与机器人"},
    {"id": "mila", "name": "Mila", "aliases": ("Mila", "Mila - Quebec AI Institute", "Quebec AI Institute"), "kind": "university_lab", "focus": "深度学习、生成模型、Agent 与 AI 安全"},
    {"id": "princeton-ai", "name": "Princeton AI Lab", "aliases": ("Princeton AI Lab", "Princeton Artificial Intelligence Lab"), "kind": "university_lab", "focus": "基础模型、视觉、语言与机器人"},
    {"id": "harvard-kempner", "name": "Kempner Institute", "aliases": ("Kempner Institute", "Harvard Kempner Institute"), "kind": "university_lab", "focus": "基础智能、神经科学与大模型"},
    {"id": "oxford-vgg", "name": "Oxford Visual Geometry Group", "aliases": ("Oxford Visual Geometry Group", "Oxford VGG", "VGG Oxford"), "kind": "university_lab", "focus": "视觉、视频、3D 与多模态"},
    {"id": "princeton-pli", "name": "Princeton Language and Intelligence", "aliases": ("Princeton Language and Intelligence", "Princeton PLI"), "kind": "university_lab", "focus": "基础模型、语言、推理与 Agent"},
    {"id": "alberta-rlai", "name": "Alberta RLAI", "aliases": ("Alberta RLAI", "Reinforcement Learning and Artificial Intelligence Lab"), "kind": "university_lab", "focus": "强化学习与持续智能"},
    {"id": "eth-rsl", "name": "ETH Robotic Systems Lab", "aliases": ("ETH Robotic Systems Lab", "Robotic Systems Lab ETH Zurich"), "kind": "university_lab", "focus": "机器人学习、运动控制与具身智能"},
    {"id": "ucl-gatsby", "name": "UCL Gatsby Computational Neuroscience Unit", "aliases": ("UCL Gatsby Computational Neuroscience Unit", "Gatsby Computational Neuroscience Unit"), "kind": "university_lab", "focus": "学习理论、神经科学与强化学习"},
    {"id": "mpi-is", "name": "Max Planck Institute for Intelligent Systems", "aliases": ("Max Planck Institute for Intelligent Systems", "MPI-IS"), "kind": "independent_lab", "focus": "机器人、视觉与智能系统"},
    {"id": "tsinghua-tsail", "name": "Tsinghua TSAIL", "aliases": ("Tsinghua TSAIL", "TSAIL Group", "清华大学人工智能研究院基础理论研究中心"), "kind": "university_lab", "focus": "机器学习、概率模型、RL 与生成模型"},
    {"id": "tsinghua-nlp", "name": "TsinghuaNLP", "aliases": ("TsinghuaNLP", "Tsinghua NLP Lab", "清华大学自然语言处理实验室", "清华大学自然语言处理与社会人文计算实验室"), "kind": "university_lab", "focus": "语言模型、知识、Agent 与 OpenBMB"},
    {"id": "tsinghua-keg", "name": "Tsinghua KEG", "aliases": ("Tsinghua KEG", "清华知识工程研究室"), "kind": "university_lab", "focus": "知识图谱、基础模型与 Agent"},
    {"id": "tsinghua-evar", "name": "Tsinghua EVAR Lab", "aliases": ("Tsinghua EVAR Lab", "EVAR Lab", "Embodied Vision and Robotics Lab", "清华具身视觉与机器人实验室"), "kind": "university_lab", "focus": "机器人学习、VLA 与具身智能"},
    {"id": "tsinghua-eir", "name": "Tsinghua Institute for Embodied Intelligence and Robotics", "aliases": ("Tsinghua Institute for Embodied Intelligence and Robotics", "清华大学具身智能与机器人研究院"), "kind": "university_lab", "focus": "具身大脑、交互、控制与本体协同"},
    {"id": "tsinghua-thbi", "name": "Tsinghua Laboratory of Brain and Intelligence", "aliases": ("Tsinghua Laboratory of Brain and Intelligence", "THBI", "清华大学脑与智能实验室"), "kind": "university_lab", "focus": "基础智能、类脑学习与具身智能"},
    {"id": "pku-eir", "name": "PKU Center for Embodied Intelligence and Robotics", "aliases": ("PKU Center for Embodied Intelligence and Robotics", "北京大学具身智能与机器人研究中心"), "kind": "university_lab", "focus": "具身智能、机器人与通用智能体"},
    {"id": "pku-epic", "name": "PKU EPIC Lab", "aliases": ("PKU EPIC Lab", "Embodied Perception and Interaction Lab"), "kind": "university_lab", "focus": "3D 视觉、VLA 与机器人学习"},
    {"id": "pku-agibot", "name": "PKU-Agibot Lab", "aliases": ("PKU-Agibot Lab", "Peking University Agibot Lab"), "kind": "university_lab", "focus": "机器人学习与具身智能"},
    {"id": "pku-cfcs", "name": "PKU Center on Frontiers of Computing Studies", "aliases": ("PKU Center on Frontiers of Computing Studies", "PKU CFCS", "北京大学前沿计算研究中心"), "kind": "university_lab", "focus": "基础模型、视觉、机器人与通用智能"},
    {"id": "casia", "name": "CASIA Multimodal AI Systems Lab", "aliases": ("Institute of Automation Chinese Academy of Sciences", "CASIA", "多模态人工智能系统全国重点实验室", "中国科学院自动化研究所"), "kind": "independent_lab", "focus": "多模态、机器人、类脑智能与具身智能"},
    {"id": "zju-cadcg", "name": "ZJU State Key Lab of CAD&CG", "aliases": ("State Key Lab of CAD&CG Zhejiang University", "浙江大学 CAD&CG 全国重点实验室"), "kind": "university_lab", "focus": "3D 视觉、空间智能与 World Model"},
    {"id": "sjtu-mvig", "name": "SJTU MVIG", "aliases": ("SJTU MVIG", "Machine Vision and Intelligence Group", "上海交通大学机器视觉与智能实验室"), "kind": "university_lab", "focus": "视觉、具身智能、机器人与生成模型"},
    {"id": "cuhk-mmlab", "name": "CUHK MMLab", "aliases": ("CUHK MMLab", "Multimedia Laboratory CUHK", "香港中文大学多媒体实验室"), "kind": "university_lab", "focus": "视觉、生成模型、多模态与机器人"},
    {"id": "hku-mmlab", "name": "HKU MMLab", "aliases": ("HKU MMLab", "MMLab@HKU"), "kind": "university_lab", "focus": "视觉、多模态与生成模型"},
)


# “已建立”只表示发布者身份与长期产出可核验，不替代技术硬门槛。未列入
# 此集合的组织仍在监测层，出现正式报告、复现或独立讨论时照常进入分析。
ESTABLISHED_ORGANIZATION_IDS = frozenset(
    {
        "openai", "anthropic", "google-deepmind", "google-research", "meta-fair",
        "microsoft-research", "nvidia-research", "apple-ml", "amazon-science",
        "ibm-research", "xai", "mistral", "cohere-labs", "ai2",
        "world-labs", "physical-intelligence", "wayve", "runway", "sakana",
        "toyota-ri", "futurehouse", "arc-institute", "microsoft-ai4science",
        "baidu", "tencent-hunyuan", "bytedance-seed",
        "alibaba-qwen", "moonshot", "zhipu", "deepseek", "modelbest",
        "stepfun", "minimax", "sensetime", "huawei-noah", "shanghai-ai-lab",
        "baai", "bigai", "pengcheng-lab", "shanghai-qizhi", "casia",
        "bair", "berkeley-rll", "stanford-sail", "stanford-svl",
        "stanford-crfm", "stanford-iris", "mit-csail", "mit-improbable",
        "cmu-ri", "cmu-real", "cmu-pathak", "nyu-cilvr", "mila",
        "princeton-ai", "princeton-pli", "harvard-kempner", "oxford-vgg",
        "alberta-rlai", "eth-rsl", "ucl-gatsby", "mpi-is",
        "tsinghua-tsail", "tsinghua-nlp", "tsinghua-keg", "tsinghua-evar",
        "tsinghua-eir", "tsinghua-thbi", "pku-eir", "pku-epic", "pku-agibot",
        "pku-cfcs", "sjtu-mvig",
        "zju-cadcg", "cuhk-mmlab", "hku-mmlab",
    }
)


RESEARCHERS: tuple[dict, ...] = (
    {"name": "Yann LeCun", "aliases": ("Yann LeCun", "Yann A. LeCun"), "focus": "自监督、JEPA 与 World Model"},
    {"name": "Fei-Fei Li", "aliases": ("Fei-Fei Li", "Li Fei-Fei", "李飞飞"), "focus": "视觉、空间智能与 World Model"},
    {"name": "Kaiming He", "aliases": ("Kaiming He", "何恺明"), "focus": "架构、表示学习与 World Model"},
    {"name": "Yoshua Bengio", "aliases": ("Yoshua Bengio",), "focus": "深度学习、生成模型与安全"},
    {"name": "Geoffrey Hinton", "aliases": ("Geoffrey Hinton", "Geoffrey E. Hinton"), "focus": "深度学习与新学习机制"},
    {"name": "Richard Sutton", "aliases": ("Richard Sutton", "Richard S. Sutton"), "focus": "强化学习与持续智能"},
    {"name": "Demis Hassabis", "aliases": ("Demis Hassabis",), "focus": "通用智能、RL 与科学智能"},
    {"name": "David Silver", "aliases": ("David Silver",), "focus": "强化学习、搜索与推理"},
    {"name": "Ilya Sutskever", "aliases": ("Ilya Sutskever",), "focus": "大模型架构与超级智能"},
    {"name": "Noam Brown", "aliases": ("Noam Brown",), "focus": "多智能体、搜索与 reasoning"},
    {"name": "Oriol Vinyals", "aliases": ("Oriol Vinyals",), "focus": "大模型、Agent 与多模态"},
    {"name": "Sergey Levine", "aliases": ("Sergey Levine",), "focus": "机器人学习、RL 与 VLA"},
    {"name": "Pieter Abbeel", "aliases": ("Pieter Abbeel",), "focus": "机器人学习与通用机器人"},
    {"name": "Ken Goldberg", "aliases": ("Ken Goldberg", "Kenneth Goldberg"), "focus": "机器人操作、抓取与人机协作"},
    {"name": "Danfei Xu", "aliases": ("Danfei Xu",), "focus": "机器人学习、VLA 与具身智能"},
    {"name": "Chelsea Finn", "aliases": ("Chelsea Finn",), "focus": "元学习、机器人与基础策略"},
    {"name": "Jitendra Malik", "aliases": ("Jitendra Malik",), "focus": "视觉、具身智能与机器人"},
    {"name": "Trevor Darrell", "aliases": ("Trevor Darrell",), "focus": "视觉、适应与具身智能"},
    {"name": "Jiajun Wu", "aliases": ("Jiajun Wu",), "focus": "3D、物理推理与 World Model"},
    {"name": "Shuran Song", "aliases": ("Shuran Song",), "focus": "机器人、3D 与具身智能"},
    {"name": "Ruslan Salakhutdinov", "aliases": ("Ruslan Salakhutdinov",), "focus": "生成模型、World Model 与 Agent"},
    {"name": "Linxi Jim Fan", "aliases": ("Linxi Jim Fan", "Linxi Fan", "Jim Fan"), "focus": "具身 Agent、机器人基础模型与仿真"},
    {"name": "Yuke Zhu", "aliases": ("Yuke Zhu", "朱裕科"), "focus": "机器人基础模型、VLA 与仿真"},
    {"name": "Danijar Hafner", "aliases": ("Danijar Hafner",), "focus": "Dreamer 系列与 World Model RL"},
    {"name": "David Ha", "aliases": ("David Ha",), "focus": "World Models、生成与开放研究"},
    {"name": "Saining Xie", "aliases": ("Saining Xie", "谢赛宁"), "focus": "视觉架构、自监督与多模态"},
    {"name": "Rob Fergus", "aliases": ("Rob Fergus", "Robert Fergus"), "focus": "自监督、视觉与 World Model"},
    {"name": "Percy Liang", "aliases": ("Percy Liang",), "focus": "基础模型、评测与 Agent"},
    {"name": "Christopher Manning", "aliases": ("Christopher Manning", "Christopher D. Manning"), "focus": "语言模型与推理"},
    {"name": "Yejin Choi", "aliases": ("Yejin Choi",), "focus": "常识、推理与开放模型"},
    {"name": "Dawn Song", "aliases": ("Dawn Song", "宋晓冬"), "focus": "Agent、安全与可信 AI"},
    {"name": "Jun Zhu", "aliases": ("Jun Zhu", "朱军"), "focus": "概率机器学习、RL 与生成模型"},
    {"name": "Yang Gao", "aliases": ("Yang Gao", "高阳"), "focus": "机器人学习、VLA 与具身智能"},
    {"name": "Zhiyuan Liu", "aliases": ("Zhiyuan Liu", "刘知远"), "focus": "语言模型、知识与 OpenBMB"},
    {"name": "Jie Tang", "aliases": ("Jie Tang", "唐杰"), "focus": "知识、基础模型与 GLM"},
    {"name": "Song-Chun Zhu", "aliases": ("Song-Chun Zhu", "Songchun Zhu", "朱松纯"), "focus": "认知架构、World Model 与通用智能"},
    {"name": "Ya-Qin Zhang", "aliases": ("Ya-Qin Zhang", "Yaqin Zhang", "张亚勤"), "focus": "智能产业、具身智能与自主系统"},
    {"name": "Zhilin Yang", "aliases": ("Zhilin Yang", "杨植麟"), "focus": "Transformer-XL、长上下文与基础模型"},
    {"name": "Daxin Jiang", "aliases": ("Daxin Jiang", "姜大昕"), "focus": "基础模型、多模态与 StepFun"},
    {"name": "Qiang Yang", "aliases": ("Qiang Yang", "杨强"), "focus": "迁移学习、联邦学习与 Agent"},
    {"name": "Zhi-Hua Zhou", "aliases": ("Zhi-Hua Zhou", "Zhihua Zhou", "周志华"), "focus": "机器学习理论与新学习范式"},
    {"name": "Hang Li", "aliases": ("Hang Li", "李航"), "focus": "语言、检索、机器学习与基础模型"},
    {"name": "Tie-Yan Liu", "aliases": ("Tie-Yan Liu", "Tieyan Liu", "刘铁岩"), "focus": "机器学习、强化学习与科学智能"},
    {"name": "Jian Sun", "aliases": ("Jian Sun", "孙剑"), "focus": "视觉架构、优化与基础模型"},
    {"name": "Xiaogang Wang", "aliases": ("Xiaogang Wang", "王晓刚"), "focus": "视觉、多模态与具身智能"},
    {"name": "Dahua Lin", "aliases": ("Dahua Lin", "林达华"), "focus": "视觉、多模态与开放基础设施"},
    {"name": "Ziwei Liu", "aliases": ("Ziwei Liu", "刘子纬"), "focus": "生成视觉、3D 与 World Model"},
    {"name": "Cewu Lu", "aliases": ("Cewu Lu", "卢策吾"), "focus": "具身智能、机器人与行为基础模型"},
    {"name": "Weinan E", "aliases": ("Weinan E", "E Weinan", "鄂维南"), "focus": "AI for Science 与新型模型"},
    {"name": "David Baker", "aliases": ("David Baker",), "focus": "蛋白质设计、结构生物学与生成模型"},
    {"name": "John Jumper", "aliases": ("John Jumper", "John M. Jumper"), "focus": "蛋白结构预测与科学智能"},
    {"name": "Pushmeet Kohli", "aliases": ("Pushmeet Kohli",), "focus": "AI for Science、可靠 AI 与基础模型"},
    {"name": "Christopher Bishop", "aliases": ("Christopher Bishop", "Christopher M. Bishop"), "focus": "机器学习与 AI for Science"},
    {"name": "Patrick Hsu", "aliases": ("Patrick Hsu", "Patrick D. Hsu"), "focus": "基因组工程、生物基础模型与实验验证"},
    {"name": "Brian Hie", "aliases": ("Brian Hie", "Brian L. Hie"), "focus": "蛋白与基因组基础模型、生成生物学"},
    {"name": "Maosong Sun", "aliases": ("Maosong Sun", "孙茂松"), "focus": "中文 NLP 与语言模型"},
    {"name": "Mido Assran", "aliases": ("Mido Assran",), "focus": "JEPA、自监督与 World Model"},
    {"name": "Jianfeng Gao", "aliases": ("Jianfeng Gao", "高剑峰"), "focus": "Agent、语言模型与交互智能"},
    {"name": "Ece Kamar", "aliases": ("Ece Kamar",), "focus": "Agent、人机协作与可靠 AI"},
    {"name": "Sanja Fidler", "aliases": ("Sanja Fidler",), "focus": "3D、生成模型与机器人"},
    {"name": "Dieter Fox", "aliases": ("Dieter Fox",), "focus": "机器人、感知与 Physical AI"},
    {"name": "Aidan Gomez", "aliases": ("Aidan Gomez",), "focus": "Transformer、多语言与基础模型"},
    {"name": "Joelle Pineau", "aliases": ("Joelle Pineau", "Joëlle Pineau"), "focus": "强化学习、开放研究与医疗 AI"},
    {"name": "Ranjay Krishna", "aliases": ("Ranjay Krishna",), "focus": "空间智能、视觉与 World Model"},
    {"name": "Russ Tedrake", "aliases": ("Russ Tedrake", "Russell Tedrake"), "focus": "机器人、控制与具身智能"},
    {"name": "Deepak Pathak", "aliases": ("Deepak Pathak",), "focus": "自监督、机器人学习与 World Model"},
    {"name": "Abhinav Gupta", "aliases": ("Abhinav Gupta",), "focus": "互联网规模视觉学习与机器人"},
    {"name": "Marco Hutter", "aliases": ("Marco Hutter",), "focus": "腿足机器人、强化学习与具身智能"},
    {"name": "Andrew Zisserman", "aliases": ("Andrew Zisserman",), "focus": "视觉、视频与多模态学习"},
    {"name": "He Wang", "aliases": ("He Wang", "王鹤"), "focus": "3D、VLA 与机器人泛化"},
    {"name": "Hao Dong", "aliases": ("Hao Dong", "董豪"), "focus": "具身 AI、强化学习与机器人"},
    {"name": "Hao Tang", "aliases": ("Hao Tang", "唐昊"), "focus": "生成模型、World Model 与空间智能"},
    {"name": "Yizhou Wang", "aliases": ("Yizhou Wang", "王亦洲"), "focus": "视觉、认知计算与通用智能"},
    {"name": "Baoquan Chen", "aliases": ("Baoquan Chen", "陈宝权"), "focus": "3D 视觉、空间智能与具身智能"},
    {"name": "Yao Mu", "aliases": ("Yao Mu", "穆尧"), "focus": "多模态具身智能与机器人学习"},
    {"name": "Guofeng Zhang", "aliases": ("Guofeng Zhang", "章国锋"), "focus": "SLAM、3D 重建与空间 World Model"},
    {"name": "Hong Qiao", "aliases": ("Hong Qiao", "乔红"), "focus": "机器人决策、感知与控制"},
    {"name": "Yi Zeng", "aliases": ("Yi Zeng", "曾毅"), "focus": "类脑智能与认知学习"},
)


RESEARCH_SOURCES: tuple[dict, ...] = (
    # tier=established：持续发布正式研究/Technical Report 的官方索引。
    {"url": "https://openai.com/research/index/", "owner": "openai", "tier": "established"},
    {"url": "https://www.anthropic.com/research", "owner": "anthropic", "tier": "established"},
    {"url": "https://deepmind.google/research/", "owner": "google-deepmind", "tier": "established", "allowed_domains": ("deepmind.google", "storage.googleapis.com")},
    {"url": "https://ai.meta.com/research/publications/", "owner": "meta-fair", "tier": "established", "allowed_domains": ("ai.meta.com",)},
    {"url": "https://www.microsoft.com/en-us/research/lab/ai-frontiers/publications/", "owner": "microsoft-research", "tier": "established"},
    {"url": "https://research.nvidia.com/labs/gear/publications/", "owner": "nvidia-research", "tier": "established", "allowed_domains": ("research.nvidia.com", "nvidia.com")},
    {"url": "https://mistral.ai/news/?category=research", "owner": "mistral", "tier": "established"},
    {"url": "https://cohere.com/research", "owner": "cohere-labs", "tier": "established"},
    {"url": "https://allenai.org/news", "owner": "ai2", "tier": "established"},
    {"url": "https://www.worldlabs.ai/blog", "owner": "world-labs", "tier": "established"},
    {"url": "https://www.pi.website/research", "owner": "physical-intelligence", "tier": "established"},
    {"url": "https://runwayml.com/research/publications", "owner": "runway", "tier": "established"},
    {"url": "https://pub.sakana.ai/", "owner": "sakana", "tier": "established"},
    {"url": "https://www.futurehouse.org/research", "owner": "futurehouse", "tier": "established"},
    {"url": "https://arcinstitute.org/publications", "owner": "arc-institute", "tier": "established", "allowed_domains": ("arcinstitute.org",)},
    {"url": "https://www.isomorphiclabs.com/articles", "owner": "isomorphic-labs", "tier": "verified"},
    {"url": "https://www.lila.ai/tech", "owner": "lila-sciences", "tier": "verified"},
    {"url": "https://www.microsoft.com/en-us/research/lab/microsoft-research-ai-for-science/", "owner": "microsoft-ai4science", "tier": "established", "allowed_domains": ("microsoft.com",)},
    # 大型综合研究页：主动召回，但不能仅凭入口直接晋级。
    {"url": "https://research.google/pubs/", "owner": "google-research", "tier": "verified"},
    {"url": "https://machinelearning.apple.com/", "owner": "apple-ml", "tier": "verified"},
    {"url": "https://www.amazon.science/publications/", "owner": "amazon-science", "tier": "verified"},
    {"url": "https://research.ibm.com/topics/foundation-models", "owner": "ibm-research", "tier": "verified"},
    {"url": "https://x.ai/news", "owner": "xai", "tier": "verified"},
    # 中国官方研究/Technical Report 索引。
    {"url": "https://ernie.baidu.com/blog/", "owner": "baidu", "tier": "established"},
    {"url": "https://seed.bytedance.com/en/research", "owner": "bytedance-seed", "tier": "established"},
    {"url": "https://qwenlm.github.io/publication/", "owner": "alibaba-qwen", "tier": "established"},
    {"url": "https://www.moonshot.ai/", "owner": "moonshot", "tier": "established"},
    {"url": "https://z.ai/blog", "owner": "zhipu", "tier": "established", "allowed_domains": ("z.ai", "zhipuai.cn")},
    {"url": "https://www.deepseek.com/en/transparency/", "owner": "deepseek", "tier": "established", "allowed_domains": ("deepseek.com", "github.com")},
    {"url": "https://chat.stepfun.com/research/en", "owner": "stepfun", "tier": "established", "allowed_domains": ("stepfun.com", "chat.stepfun.com", "github.com")},
    {"url": "https://www.minimax.io/blog", "owner": "minimax", "tier": "established"},
    {"url": "https://modelbest.cn/", "owner": "modelbest", "tier": "established"},
    {"url": "https://arc.tencent.com/research", "owner": "tencent-hunyuan", "tier": "verified"},
    {"url": "https://www.noahlab.com.hk/research_paper", "owner": "huawei-noah", "tier": "verified"},
    # 具名实验室索引只提高召回，普通论文仍需技术与外部承接验证。
    {"url": "https://bair.berkeley.edu/blog/", "owner": "bair", "tier": "verified"},
    {"url": "https://crfm.stanford.edu/", "owner": "stanford-crfm", "tier": "verified"},
    {"url": "https://www.ri.cmu.edu/publications/", "owner": "cmu-ri", "tier": "verified"},
    {"url": "https://wp.nyu.edu/cilvr/cilvr-group-publications/", "owner": "nyu-cilvr", "tier": "verified"},
)


ORGANIZATIONS_BY_ID = {item["id"]: item for item in ORGANIZATIONS}
SOURCE_BY_URL = {item["url"].rstrip("/"): item for item in RESEARCH_SOURCES}


def default_priority_pages() -> list[str]:
    return [item["url"] for item in RESEARCH_SOURCES]


def default_organization_aliases() -> list[str]:
    return [
        alias
        for item in ORGANIZATIONS
        if item["id"] in ESTABLISHED_ORGANIZATION_IDS
        for alias in item["aliases"]
    ]


def default_monitored_organization_aliases() -> list[str]:
    return [
        alias
        for item in ORGANIZATIONS
        if item["id"] not in ESTABLISHED_ORGANIZATION_IDS
        for alias in item["aliases"]
    ]


def default_researcher_aliases() -> list[str]:
    return [alias for item in RESEARCHERS for alias in item["aliases"]]


def source_record(url: str) -> dict | None:
    return SOURCE_BY_URL.get(url.rstrip("/"))


def organization_record(organization_id: str) -> dict | None:
    return ORGANIZATIONS_BY_ID.get(organization_id)


def organization_tier(organization_id: str) -> str:
    return (
        "established"
        if organization_id in ESTABLISHED_ORGANIZATION_IDS
        else "monitored"
    )
