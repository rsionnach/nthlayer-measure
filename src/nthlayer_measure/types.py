"""Shared data types for the nthlayer-measure pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class AutonomyLevel(Enum):
    """Agent autonomy levels managed by governance."""

    FULL = "full"
    SUPERVISED = "supervised"
    ADVISORY_ONLY = "advisory_only"
    SUSPENDED = "suspended"


@dataclass(frozen=True)
class AgentOutput:
    """Normalized output from any adapter — the universal input to evaluation."""

    agent_name: str
    task_id: str
    output_content: str
    output_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class QualityScore:
    """Complete evaluation result for a single agent output."""

    eval_id: str
    agent_name: str
    task_id: str
    dimensions: dict[str, float]
    reasoning: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    evaluator_model: str = ""
    cost_usd: float | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class GovernanceAction:
    """Action taken by the governance engine."""

    agent_name: str
    target_level: AutonomyLevel
    reason: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class TrendWindow:
    """Aggregated trend data over a time window."""

    agent_name: str
    window_days: int
    dimension_averages: dict[str, float]
    evaluation_count: int
    confidence_mean: float
    reversal_rate: float = 0.0
    total_cost_usd: float = 0.0
    avg_cost_per_eval: float = 0.0
