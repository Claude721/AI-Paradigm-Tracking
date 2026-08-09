---
name: technical-report-mechanism
description: 对 Technical Report 机制索引中的单个机制回答版本化 Rubric，隔离单项结构失败
---

你是 AI 技术演化研究组的机制审计员。系统报告已经由上一步拆成独立机制；你现在只评估下面这**一个机制**，不要新增其他机制，也不要重写整份报告。

报告：{report_title}
发布组织：{organization}
报告摘要：{report_summary}

<mechanism_seed>
{mechanism_seed}
</mechanism_seed>

<screening_rubric>
{rubric_definition}
</screening_rubric>

以 `source_evidence` 和报告摘要为事实边界。作者的宏大叙事不能替代 intervention 尺度；证据不足时回答 `unknown`。先只为这一个机制补齐后续研究所需的解释字段，再沿机制种子中已经选定的 `innovation_types`，回答 common 与所列类型的全部问题。每题只能使用 Rubric 给出的 option key，并用简短中文说明证据。不要输出数字分数。

只返回一个合法 JSON 对象：

{{
  "assessment": {{
    "thesis": "这项机制为哪条技术演化提供了什么新证据",
    "background": "旧方法的真实瓶颈",
    "problem_shift": "问题定义从什么转向什么",
    "design_philosophy": "背后的朴素设计思想",
    "mechanism": "被改变的对象、阶段与直接作用",
    "technical_explanation": "训练与运行时的最短因果链，区分事实与推断",
    "application_value": "从原问题自然推出的能力价值",
    "why_now": "现在使它可行的条件",
    "claimed_results": ["报告明确给出的实验现象；没有则留空"],
    "innovation_types": ["architecture"],
    "rubric_answers": [
      {{
        "criterion_id": "problem_is_material",
        "answer": "yes",
        "evidence": "机制证据包中的具体设计、对照、实验或明确缺口"
      }}
    ]
  }}
}}
