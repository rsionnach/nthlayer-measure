"""Governance engine — watches error budgets, manages agent autonomy.

Key constraint: can REDUCE autonomy, never increase without human approval
(one-way safety ratchet). Delegates governance judgment to the model (ZFC).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol

from nthlayer_measure.store.protocol import ScoreStore
from nthlayer_measure.trends.tracker import TrendTracker
from nthlayer_measure.types import AutonomyLevel, GovernanceAction, TrendWindow

logger = logging.getLogger(__name__)


class GovernanceEngine(Protocol):
    """Manages agent autonomy levels based on evaluation trends."""

    async def check_agent(self, agent_name: str) -> GovernanceAction | None: ...

    async def get_autonomy(self, agent_name: str) -> AutonomyLevel: ...

    async def restore_autonomy(self, agent_name: str, level: AutonomyLevel, approver: str) -> None:
        """Restore autonomy — requires human approver (safety ratchet)."""
        ...


class ErrorBudgetGovernance:
    """Governance based on error budget consumption over a rolling window.

    The governance DECISION is judgment — it goes to the model (ZFC).
    The governance ACTION (persisting reduced autonomy) is transport — it stays in code.
    """

    def __init__(
        self,
        store: ScoreStore,
        tracker: TrendTracker,
        window_days: int = 7,
        threshold: float = 0.5,
        model: str | None = None,
        max_tokens: int = 1024,
    ) -> None:
        self._store = store
        self._tracker = tracker
        self._window_days = window_days
        self._threshold = threshold
        self._model = model
        self._max_tokens = max_tokens
        self._client = None

    def _get_client(self):
        """Lazy-init the Anthropic client."""
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic()
        return self._client

    def build_governance_prompt(
        self, agent_name: str, trend: TrendWindow, current_level: AutonomyLevel
    ) -> str:
        """Construct the governance judgment prompt.

        The threshold is operator context ('the operator considers this concerning'),
        not a hard trigger. The model decides whether action is warranted.
        """
        dims = "\n".join(
            f"  - {name}: {avg:.3f}" for name, avg in trend.dimension_averages.items()
        )
        return f"""You are a governance advisor for an AI agent quality monitoring system.

## Agent Trend Data
- Agent: {agent_name}
- Window: {trend.window_days} days
- Evaluation count: {trend.evaluation_count}
- Confidence mean: {trend.confidence_mean:.3f}
- Reversal rate: {trend.reversal_rate:.3f}
- Current autonomy level: {current_level.value}
- Dimension averages:
{dims}

## Operator Preferences
- The operator considers dimension scores below {self._threshold} to be concerning
- Error budget window: {self._window_days} days

## Decision
Based on the trend data and operator preferences, should this agent's autonomy be reduced one level (from {current_level.value} to {self._reduce_level(current_level).value})?

Consider:
- Is the degradation significant enough to warrant action, or is it normal variance?
- Which dimensions are underperforming and how critical might they be?
- Does the evaluation count provide a statistically meaningful sample?
- Is the current autonomy level already appropriate for the observed performance?

Respond with valid JSON only:
{{
  "should_reduce": <bool>,
  "reason": "<brief explanation of your decision>"
}}"""

    def parse_governance_response(self, raw: str) -> tuple[bool, str]:
        """Parse model governance response. Returns (should_reduce, reason)."""
        from nthlayer_measure._parsing import strip_markdown_fences

        text = strip_markdown_fences(raw)
        data = json.loads(text)
        return bool(data.get("should_reduce", False)), str(data.get("reason", ""))

    async def check_agent(self, agent_name: str) -> GovernanceAction | None:
        trend = await self._tracker.compute_window(agent_name, self._window_days)

        if trend.evaluation_count == 0:
            return None

        # ZFC: governance judgment requires a model. No model → no opinion.
        if self._model is None:
            logger.debug("No governance model configured, skipping judgment for %s", agent_name)
            return None

        current = await self.get_autonomy(agent_name)
        reduced = self._reduce_level(current)
        if reduced == current:
            return None  # Already at lowest level

        try:
            prompt = self.build_governance_prompt(agent_name, trend, current)
            client = self._get_client()
            response = await asyncio.wait_for(
                client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=60.0,
            )
            if not response.content:
                logger.warning("Governance model returned empty content for %s", agent_name)
                return None
            should_reduce, reason = self.parse_governance_response(response.content[0].text)
        except Exception:
            # ZFC: fail open on model unavailability — no governance opinion
            logger.warning("Governance model call failed for %s, failing open", agent_name, exc_info=True)
            return None

        if not should_reduce:
            return None

        await self._store.set_autonomy(
            agent_name, reduced.value, "governance:error_budget"
        )
        return GovernanceAction(
            agent_name=agent_name,
            target_level=reduced,
            reason=reason,
        )

    async def get_autonomy(self, agent_name: str) -> AutonomyLevel:
        level_str = await self._store.get_autonomy(agent_name)
        if level_str is None:
            return AutonomyLevel.FULL
        return AutonomyLevel(level_str)

    async def restore_autonomy(
        self, agent_name: str, level: AutonomyLevel, approver: str
    ) -> None:
        if not approver:
            raise ValueError("Safety ratchet: approver is required to restore autonomy")
        await self._store.set_autonomy(agent_name, level.value, approver)

    @staticmethod
    def _reduce_level(current: AutonomyLevel) -> AutonomyLevel:
        """One step down the autonomy ladder."""
        reduction = {
            AutonomyLevel.FULL: AutonomyLevel.SUPERVISED,
            AutonomyLevel.SUPERVISED: AutonomyLevel.ADVISORY_ONLY,
            AutonomyLevel.ADVISORY_ONLY: AutonomyLevel.SUSPENDED,
            AutonomyLevel.SUSPENDED: AutonomyLevel.SUSPENDED,
        }
        return reduction[current]
