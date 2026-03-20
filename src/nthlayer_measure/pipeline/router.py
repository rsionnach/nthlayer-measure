"""Pipeline router — connects adapters to evaluators to stores."""

from __future__ import annotations

import asyncio

from nthlayer_measure.adapters.protocol import Adapter
from nthlayer_measure.detection.protocol import DegradationDetector
from nthlayer_measure.governance.engine import GovernanceEngine
from nthlayer_measure.pipeline.evaluator import Evaluator
from nthlayer_measure.store.protocol import ScoreStore
from nthlayer_measure.telemetry import emit_decision_event
from nthlayer_measure.trends.tracker import TrendTracker
from nthlayer_learn import create as verdict_create, VerdictStore as VerdictStoreBase

import logging

logger = logging.getLogger(__name__)

DEFAULT_APPROVE_THRESHOLD = 0.5


class PipelineRouter:
    """Routes agent output through the evaluation pipeline.

    Flow: adapter.receive() -> evaluator.evaluate() -> store.save_score()
          -> governance.check_agent() -> detector.check() -> (alerts)
    """

    def __init__(
        self,
        adapter: Adapter,
        evaluator: Evaluator,
        store: ScoreStore,
        tracker: TrendTracker,
        dimensions: list[str],
        governance: GovernanceEngine | None = None,
        detector: DegradationDetector | None = None,
        detection_window_days: int = 7,
        verdict_store: VerdictStoreBase | None = None,
        approve_threshold: float | None = None,
    ) -> None:
        self._adapter = adapter
        self._evaluator = evaluator
        self._store = store
        self._tracker = tracker
        self._dimensions = dimensions
        self._governance = governance
        self._detector = detector
        self._detection_window_days = detection_window_days
        self._verdict_store = verdict_store
        self._approve_threshold = (
            approve_threshold if approve_threshold is not None
            else DEFAULT_APPROVE_THRESHOLD
        )

    async def run(self) -> None:
        """Process agent outputs through the full pipeline."""
        async for output in self._adapter.receive():
            score = await self._evaluator.evaluate(output, self._dimensions)
            await self._store.save_score(score)

            # Create verdict if verdict store is configured (fail open)
            if self._verdict_store is not None:
                try:
                    verdict = await self._create_verdict(score)
                    await asyncio.to_thread(self._verdict_store.put, verdict)
                    await self._store.set_verdict_id(score.eval_id, verdict.id)
                except Exception:
                    logger.warning(
                        "Failed to create/store verdict for %s — continuing without verdict",
                        score.eval_id,
                        exc_info=True,
                    )

            alerts = None
            if self._detector is not None:
                window = await self._tracker.compute_window(
                    output.agent_name, self._detection_window_days
                )
                alerts = self._detector.check(window)

            emit_decision_event(score, alerts)

            if self._governance is not None:
                await self._governance.check_agent(output.agent_name)

    async def _create_verdict(self, score):
        """Map QualityScore to a verdict."""
        avg_score = sum(score.dimensions.values()) / len(score.dimensions)

        reasoning_summary = "; ".join(
            f"{name}: {reason}" for name, reason in score.reasoning.items()
        ) if score.reasoning else None

        return await asyncio.to_thread(
            verdict_create,
            subject={
                "type": "agent_output",
                "ref": score.task_id,
                "summary": f"Evaluation of {score.agent_name}: {score.task_id}",
                "agent": score.agent_name,
            },
            judgment={
                "action": (
                    "approve" if avg_score >= self._approve_threshold
                    else "reject"
                ),
                "confidence": score.confidence,
                "score": avg_score,
                "dimensions": score.dimensions,
                "reasoning": reasoning_summary,
            },
            producer={
                "system": "arbiter",
                "model": score.evaluator_model,
            },
            metadata={
                "cost_currency": score.cost_usd,
            },
        )
