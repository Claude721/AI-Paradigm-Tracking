"""结构化运行审计：记录可复核决策和用量，不保存 prompt、响应正文或私有推理。"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RunAudit:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.llm_calls: list[dict[str, Any]] = []
        self.origin_decisions: list[dict[str, Any]] = []
        self.candidate_decisions: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.last_stats: dict[str, Any] = {}

    def record_llm(
        self,
        *,
        stage: str,
        role: str,
        model: str,
        subject: str,
        response=None,
        error: Exception | str | None = None,
    ) -> None:
        usage = getattr(response, "usage", None)
        completion_details = getattr(usage, "completion_tokens_details", None)
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        self.llm_calls.append(
            {
                "stage": stage,
                "role": role,
                "model": model,
                "subject": _clean(subject, 180),
                "status": "failed" if error else "passed",
                "prompt_tokens": _integer(getattr(usage, "prompt_tokens", 0)),
                "completion_tokens": _integer(
                    getattr(usage, "completion_tokens", 0)
                ),
                "total_tokens": _integer(getattr(usage, "total_tokens", 0)),
                "reasoning_tokens": _integer(
                    getattr(completion_details, "reasoning_tokens", 0)
                ),
                "cached_tokens": _integer(
                    getattr(prompt_details, "cached_tokens", 0)
                ),
                "error": _clean(str(error), 300) if error else "",
            }
        )

    def record_origin(self, payload: dict[str, Any]) -> None:
        self.origin_decisions.append(_safe_payload(payload))

    def record_candidate(self, payload: dict[str, Any]) -> None:
        self.candidate_decisions.append(_safe_payload(payload))

    def event(self, stage: str, status: str, detail: str) -> None:
        self.events.append(
            {
                "stage": stage,
                "status": status,
                "detail": _clean(detail, 500),
            }
        )

    def write(
        self,
        stats: dict[str, Any] | None = None,
        *,
        status: str = "completed",
        output_dir: Path | str = "logs",
    ) -> dict[str, Any]:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        totals = self.token_totals()
        self.last_stats = _safe_payload(stats or {})
        payload = {
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "pipeline_stats": self.last_stats,
            "llm_summary": totals,
            "llm_calls": self.llm_calls,
            "origin_decisions": self.origin_decisions,
            "candidate_decisions": self.candidate_decisions,
            "events": self.events,
            "privacy_note": (
                "本审计不保存 prompt、模型响应正文或模型私有推理；"
                "只保存阶段、用量、结构化筛选结果与可见失败原因。"
            ),
        }
        json_path = directory / "run_audit_latest.json"
        markdown_path = directory / "run_audit_latest.md"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
        return {
            **totals,
            "audit_json_path": str(json_path),
            "audit_markdown_path": str(markdown_path),
        }

    def token_totals(self) -> dict[str, Any]:
        by_stage: dict[str, int] = defaultdict(int)
        for item in self.llm_calls:
            by_stage[str(item["stage"])] += _integer(item.get("total_tokens"))
        return {
            "llm_call_count": len(self.llm_calls),
            "llm_failed_count": sum(
                item.get("status") == "failed" for item in self.llm_calls
            ),
            "llm_prompt_tokens": sum(
                _integer(item.get("prompt_tokens")) for item in self.llm_calls
            ),
            "llm_completion_tokens": sum(
                _integer(item.get("completion_tokens")) for item in self.llm_calls
            ),
            "llm_reasoning_tokens": sum(
                _integer(item.get("reasoning_tokens")) for item in self.llm_calls
            ),
            "llm_total_tokens": sum(
                _integer(item.get("total_tokens")) for item in self.llm_calls
            ),
            "llm_tokens_by_stage": dict(sorted(by_stage.items())),
        }


def _render_markdown(payload: dict[str, Any]) -> str:
    stats = payload["pipeline_stats"]
    llm = payload["llm_summary"]
    source_counts = stats.get("source_counts") or {}
    lines = [
        "# AI 技术范式雷达｜运行审计",
        "",
        f"> 状态：{payload['status']}  ",
        f"> 开始：{payload['started_at']}  ",
        f"> 结束：{payload['finished_at']}",
        "",
        "## 本轮漏斗",
        "",
        f"- 原始材料：{stats.get('origin_count', 0)}",
        f"- 计划机制抽取：{stats.get('planned_analysis_count', 0)}",
        f"- 恢复待抽取积压：{stats.get('pending_origin_backlog_loaded', 0)}",
        f"- 实际机制抽取：{stats.get('analysis_count', 0)}",
        f"- 延后到下轮：{stats.get('analysis_deferred_count', 0)}",
        f"- 恢复待深挖路线：{stats.get('pending_deep_backlog_loaded', 0)}",
        f"- 深挖候选：{stats.get('deep_candidate_count', 0)}",
        f"- 最终可报告：{stats.get('high_value_count', 0)}",
        "",
        "## 信源返回量",
        "",
    ]
    if source_counts:
        lines.extend(f"- {name}：{count}" for name, count in source_counts.items())
    else:
        lines.append("- 本轮未写入结构化信源计数。")
    lines.extend(
        [
            "",
            "## 模型用量",
            "",
            f"- 调用：{llm['llm_call_count']} 次；失败 {llm['llm_failed_count']} 次",
            f"- 输入 tokens：{llm['llm_prompt_tokens']}",
            f"- 输出 tokens：{llm['llm_completion_tokens']}",
            f"- 其中 reasoning tokens：{llm['llm_reasoning_tokens']}",
            f"- 合计 tokens：{llm['llm_total_tokens']}",
        ]
    )
    for stage, value in llm["llm_tokens_by_stage"].items():
        lines.append(f"  - {stage}：{value}")
    lines.extend(["", "## 原始材料筛选记录", ""])
    if payload["origin_decisions"]:
        for item in payload["origin_decisions"]:
            verdict = "进入候选" if item.get("initial_gate_passed") else "未进入候选"
            reason = item.get("gate_reason") or item.get("rejection_reason") or "通过"
            lines.append(
                f"- **{item.get('title', '未命名材料')}**：{verdict}；{reason}"
            )
    else:
        lines.append("- 没有完成机制抽取。")
    lines.extend(["", "## 范式准入记录", ""])
    if payload["candidate_decisions"]:
        for item in payload["candidate_decisions"]:
            verdict = "进入报告" if item.get("reportable") else "留在观察池"
            reason = item.get("admission_reason") or item.get("rejection_reason") or ""
            lines.append(f"- **{item.get('name', '未命名路线')}**：{verdict}；{reason}")
    else:
        lines.append("- 没有形成范式级候选。")
    lines.extend(["", "## 运行事件", ""])
    if payload["events"]:
        for item in payload["events"]:
            lines.append(
                f"- `{item.get('stage')}` {item.get('status')}：{item.get('detail')}"
            )
    else:
        lines.append("- 无额外事件。")
    lines.extend(["", f"> {payload['privacy_note']}", ""])
    return "\n".join(lines)


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, Path):
            safe[key] = str(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = _clean(value, 1000) if isinstance(value, str) else value
        elif isinstance(value, dict):
            safe[key] = _safe_payload(value)
        elif isinstance(value, (list, tuple, set)):
            safe[key] = [
                _clean(item, 500) if isinstance(item, str) else item
                for item in value
            ]
        else:
            safe[key] = _clean(str(value), 500)
    return safe


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _clean(value: Any, limit: int) -> str:
    return str(value or "").replace("\x00", "")[:limit]


run_audit = RunAudit()
