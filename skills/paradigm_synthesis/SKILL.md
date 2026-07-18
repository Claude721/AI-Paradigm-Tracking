---
name: paradigm_synthesis
description: 把论文、评审、引用、复现和讨论证据综合为技术范式级结论
---

你要把论文级候选综合为一条技术范式结论。不能因为引用或点赞多就认定范式成立，也不能因为声量小就否定一个机制清楚、实验扎实且外延广的新方向。

暂定名称：{provisional_name}
暂定判断：{provisional_thesis}
问题变化：{problem_shift}
核心机制：{mechanism}
上一代范式：{lineage_parent}
证据列表：{evidence}

要求：
1. 区分原始作者自证、同行评议、独立实现/复现、引用、社区讨论；不要把它们混成一个“热度”。
2. 说明目前是“单点突破”“多团队承接”还是“跨平台扩散”，并指出证据缺口。
3. 若 GitHub/HN 结果只是同名误匹配，在 evidence_assessment 中明确排除，不把它计入趋势结论。
4. 技术谱系必须给出关键能力边界的迁移，不能只列模型名字。
5. 不增加证据列表之外的事实和数字。

只返回 JSON：
{{
  "name": "稳定、机制级的范式名称",
  "thesis": "一句话范式判断",
  "problem_shift": "旧问题定义到新问题定义",
  "mechanism": "综合后的核心机制",
  "why_now": "现在成立的技术前提",
  "lineage_path": ["上一代范式", "关键中间节点", "当前范式"],
  "evidence_assessment": "哪些证据扎实、哪些只是声量或可能误匹配",
  "trend_interpretation": "当前扩散阶段与加速/未加速的判断",
  "open_questions": ["仍需验证的关键问题"]
}}
