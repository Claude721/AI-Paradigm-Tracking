"""Time-aware service for current research, route updates and backfill.

Entity snapshots in SQLite remain the durable queues. This controller only
allocates the current process's time; an unvisited entity stays pending.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


LANE_WEIGHTS = {"current": 6, "updates": 2, "backfill": 1}


@dataclass
class ResearchLaneScheduler:
    deadline: float
    deep_reserve_seconds: float = 0
    clock: Callable[[], float] = time.monotonic
    started_at: float = field(init=False)
    seconds: dict[str, float] = field(init=False)
    operations: dict[str, int] = field(init=False)
    deep_seconds: float = 0

    def __post_init__(self) -> None:
        self.started_at = self.clock()
        self.seconds = dict.fromkeys(LANE_WEIGHTS, 0.0)
        self.operations = dict.fromkeys(LANE_WEIGHTS, 0)

    def choose(self, active: set[str]) -> str | None:
        if not active or self.clock() >= self.deadline:
            return None
        # Actual elapsed time matters more than item counts: one report can
        # cost far more than a batch of ordinary-paper eligibility checks.
        return min(
            active,
            key=lambda lane: (
                self.seconds[lane] / LANE_WEIGHTS[lane],
                self.operations[lane] / LANE_WEIGHTS[lane],
                list(LANE_WEIGHTS).index(lane),
            ),
        )

    def visit_deadline(
        self, lane: str, active: set[str], *, stage: str
    ) -> float:
        if self.deadline == float("inf"):
            return self.deadline
        budget = max(self.deadline - self.clock(), 0.0) + sum(
            self.seconds[name] for name in active
        )
        # Empty lanes lend their share to those with work. A lane with work
        # keeps its unserved entitlement before another can spend the tail.
        weight = sum(LANE_WEIGHTS[name] for name in active)
        protected = sum(
            max(budget * LANE_WEIGHTS[name] / weight - self.seconds[name], 0.0)
            for name in active if name != lane
        ) if weight else 0.0
        if stage == "origin" and self.deep_reserve_seconds > self.deep_seconds:
            # Reserve inside this lane as well as across lanes. An initial
            # long origin call must leave time for the candidate it creates,
            # even when no deep task existed before the visit.
            lane_remaining = max(
                budget * LANE_WEIGHTS[lane] / weight - self.seconds[lane], 0.0
            ) if weight else 0.0
            protected += min(
                self.deep_reserve_seconds - self.deep_seconds,
                lane_remaining / 2,
            )
        return max(self.clock(), self.deadline - protected)

    def account(self, lane: str, started: float, *, stage: str) -> None:
        elapsed = max(self.clock() - started, 0.0)
        self.seconds[lane] += elapsed
        self.operations[lane] += 1
        if stage == "deep":
            self.deep_seconds += elapsed

    def snapshot(self) -> dict:
        return {
            "weights": dict(LANE_WEIGHTS),
            "seconds": {name: round(value, 3) for name, value in self.seconds.items()},
            "operations": dict(self.operations),
            "deep_seconds": round(self.deep_seconds, 3),
        }
