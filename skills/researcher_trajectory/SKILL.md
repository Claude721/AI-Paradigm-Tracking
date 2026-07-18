---
name: researcher_trajectory
description: 分析关键研究者的研究连续性以及其作为新范式开拓者的证据
---

你在分析一位技术范式关键作者。只依据给定的公开学术资料，判断其此前研究是否与当前范式一脉相承。

当前范式：{paradigm_name}
核心机制：{mechanism}
关键词：{keywords}
研究者：{researcher_name}
当前机构：{affiliation}
近期/代表论文：{works}

要求：
1. 区分长期连续研究、近期转向、偶然合作三种情况。
2. 指出连续性所依赖的具体论文题目或研究主题，不能靠名气推断。
3. 如果资料不足，明确写资料不足并降低评分。
4. 不推测创业意愿、私人联系方式或未公开任职。

只返回 JSON：
{{
  "trajectory_summary": "两到四句证据化结论",
  "trajectory_consistency": 0,
  "current_role_note": "从公开资料可确认的当前状态；未知则写未知"
}}
