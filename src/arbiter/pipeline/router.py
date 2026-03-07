"""Pipeline router — connects adapters to evaluators to stores."""

from __future__ import annotations

from arbiter.adapters.protocol import Adapter
from arbiter.detection.protocol import DegradationDetector
from arbiter.governance.engine import GovernanceEngine
from arbiter.pipeline.evaluator import Evaluator
from arbiter.store.protocol import ScoreStore
from arbiter.telemetry import emit_decision_event
from arbiter.trends.tracker import TrendTracker

import logging

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._adapter = adapter
        self._evaluator = evaluator
        self._store = store
        self._tracker = tracker
        self._dimensions = dimensions
        self._governance = governance
        self._detector = detector
        self._detection_window_days = detection_window_days

    async def run(self) -> None:
        """Process agent outputs through the full pipeline."""
        async for output in self._adapter.receive():
            score = await self._evaluator.evaluate(output, self._dimensions)
            await self._store.save_score(score)

            alerts = None
            if self._detector is not None:
                window = await self._tracker.compute_window(
                    output.agent_name, self._detection_window_days
                )
                alerts = self._detector.check(window)

            emit_decision_event(score, alerts)

            if self._governance is not None:
                await self._governance.check_agent(output.agent_name)
