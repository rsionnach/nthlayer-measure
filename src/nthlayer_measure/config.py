"""Configuration loading for Arbiter."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AgentConfig:
    """Configuration for a monitored agent."""

    name: str
    adapter: str = "webhook"
    dimensions: list[str] | None = None
    manifest: str | None = None
    adapter_config: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluatorConfig:
    """Configuration for the evaluation model."""

    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    temperature: float = 0.0


@dataclass
class StoreConfig:
    """Configuration for the score store."""

    backend: str = "sqlite"
    path: str = "arbiter.db"


@dataclass
class GovernanceConfig:
    """Configuration for the governance engine."""

    error_budget_window_days: int = 7
    error_budget_threshold: float = 0.5


@dataclass
class DetectionConfig:
    """Configuration for degradation detection thresholds."""

    max_reversal_rate: float = 0.3
    min_dimension_scores: dict[str, float] = field(default_factory=dict)
    min_confidence: float = 0.5


@dataclass
class VerdictConfig:
    """Configuration for verdict integration."""

    store_path: str = "verdicts.db"


@dataclass
class ArbiterConfig:
    """Top-level Arbiter configuration matching arbiter.yaml shape."""

    evaluator: EvaluatorConfig = field(default_factory=EvaluatorConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    governance: GovernanceConfig = field(default_factory=GovernanceConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    dimensions: list[str] = field(default_factory=lambda: ["correctness", "completeness", "safety"])
    agents: list[AgentConfig] = field(default_factory=list)
    verdict: VerdictConfig | None = None


def load_config(path: Path) -> ArbiterConfig:
    """Load ArbiterConfig from a YAML file."""
    raw = yaml.safe_load(path.read_text())
    if raw is None:
        return ArbiterConfig()

    def _section(key: str, cls: type):
        section = raw.get(key)
        if section is None:
            return cls()
        if not isinstance(section, dict):
            raise ValueError(f"Config section '{key}' must be a mapping, got {type(section).__name__}")
        return cls(**section)

    evaluator = _section("evaluator", EvaluatorConfig)
    store = _section("store", StoreConfig)
    governance = _section("governance", GovernanceConfig)
    detection = _section("detection", DetectionConfig)
    dimensions = raw.get("dimensions", ["correctness", "completeness", "safety"])

    agents = []
    for i, agent_data in enumerate(raw.get("agents", [])):
        if not isinstance(agent_data, dict):
            raise ValueError(f"agents[{i}] must be a mapping, got {type(agent_data).__name__}")
        if "name" not in agent_data:
            raise ValueError(f"agents[{i}] missing required field 'name'")
        agents.append(AgentConfig(
            name=agent_data["name"],
            adapter=agent_data.get("adapter", "webhook"),
            dimensions=agent_data.get("dimensions"),
            manifest=agent_data.get("manifest"),
            adapter_config=agent_data.get("adapter_config", {}),
        ))

    verdict_cfg = None
    verdict_raw = raw.get("verdict")
    if verdict_raw is not None:
        if not isinstance(verdict_raw, dict):
            raise ValueError(f"Config section 'verdict' must be a mapping, got {type(verdict_raw).__name__}")
        store_raw = verdict_raw.get("store", {})
        if not isinstance(store_raw, dict):
            raise ValueError(f"Config section 'verdict.store' must be a mapping, got {type(store_raw).__name__}")
        verdict_cfg = VerdictConfig(store_path=store_raw.get("path", "verdicts.db"))

    return ArbiterConfig(
        evaluator=evaluator,
        store=store,
        governance=governance,
        detection=detection,
        dimensions=dimensions,
        agents=agents,
        verdict=verdict_cfg,
    )
