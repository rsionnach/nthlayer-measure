"""Async evaluation queue for fire-and-forget API requests.

Not a message broker — just an asyncio queue within the server process.
Evaluations are processed by a pool of async workers.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from typing import Any

import httpx

from nthlayer_measure.api.normalise import EvaluationRequest
from nthlayer_measure.pipeline.evaluator import Evaluator
from nthlayer_measure.store.protocol import ScoreStore
from nthlayer_measure.types import AgentOutput

logger = logging.getLogger(__name__)

DEFAULT_APPROVE_THRESHOLD = 0.5
MAX_RESULTS = 10_000  # Evict oldest results beyond this limit


class EvaluationQueue:
    """Async queue that processes evaluation requests in the background."""

    def __init__(
        self,
        evaluator: Evaluator,
        store: ScoreStore,
        dimensions: list[str],
        verdict_store=None,
        approve_threshold: float = DEFAULT_APPROVE_THRESHOLD,
        max_workers: int = 5,
    ) -> None:
        self._evaluator = evaluator
        self._store = store
        self._dimensions = dimensions
        self._verdict_store = verdict_store
        self._approve_threshold = approve_threshold
        self._max_workers = max_workers
        self._queue: asyncio.Queue = asyncio.Queue()
        self._results: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._workers: list[asyncio.Task] = []

    async def start(self) -> None:
        """Spawn worker tasks."""
        for _ in range(self._max_workers):
            task = asyncio.create_task(self._worker())
            self._workers.append(task)

    async def stop(self) -> None:
        """Drain queue and cancel workers."""
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def submit(self, request: EvaluationRequest) -> str:
        """Submit an evaluation request. Returns eval_id immediately."""
        eval_id = f"eval-{uuid.uuid4().hex[:12]}"
        self._results[eval_id] = {"status": "queued"}
        # Evict oldest results to prevent unbounded memory growth
        while len(self._results) > MAX_RESULTS:
            self._results.popitem(last=False)
        await self._queue.put((eval_id, request))
        return eval_id

    async def get_result(self, eval_id: str) -> dict[str, Any]:
        """Get result by eval_id. Returns status dict."""
        return self._results.get(eval_id, {"status": "not_found"})

    async def _worker(self) -> None:
        """Process queued evaluations."""
        while True:
            eval_id, request = await self._queue.get()
            try:
                self._results[eval_id] = {"status": "evaluating"}

                agent_output = AgentOutput(
                    agent_name=request.agent_name,
                    task_id=request.task_id,
                    output_content=request.output,
                    output_type="api",
                    metadata=request.metadata,
                )

                score = await self._evaluator.evaluate(
                    agent_output, self._dimensions
                )
                await self._store.save_score(score)

                # Create verdict (fail-open, matches PipelineRouter pattern)
                verdict = None
                if self._verdict_store is not None:
                    try:
                        verdict = await self._create_verdict(score)
                        await asyncio.to_thread(
                            self._verdict_store.put, verdict
                        )
                        await self._store.set_verdict_id(
                            score.eval_id, verdict.id
                        )
                    except Exception:
                        logger.warning(
                            "Failed to create verdict for %s",
                            eval_id,
                            exc_info=True,
                        )

                self._results[eval_id] = {
                    "status": "complete",
                    "score": score,
                    "verdict": verdict,
                }

                # Fire callback if provided
                if request.callback_url:
                    await self._send_callback(
                        request.callback_url, eval_id, score, verdict
                    )

            except Exception as exc:
                logger.warning(
                    "Evaluation failed for %s: %s", eval_id, exc
                )
                self._results[eval_id] = {
                    "status": "error",
                    "error": str(exc),
                }
            finally:
                self._queue.task_done()

    async def _create_verdict(self, score):
        """Create a verdict from a QualityScore. Mirrors PipelineRouter._create_verdict."""
        from nthlayer_learn import create as verdict_create

        dims = score.dimensions or {}
        avg_score = sum(dims.values()) / len(dims) if dims else 0.0

        reasoning_summary = (
            "; ".join(f"{k}: {v}" for k, v in score.reasoning.items())
            if score.reasoning
            else None
        )

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
                    "approve"
                    if avg_score >= self._approve_threshold
                    else "reject"
                ),
                "confidence": score.confidence,
                "score": avg_score,
                "dimensions": score.dimensions,
                "reasoning": reasoning_summary,
            },
            producer={
                "system": "nthlayer-measure",
                "model": score.evaluator_model,
            },
            metadata={"cost_currency": score.cost_usd},
        )

    async def _send_callback(
        self, url: str, eval_id: str, score, verdict
    ) -> None:
        """POST verdict to callback URL. Best-effort with 3 retries."""
        from nthlayer_measure.api.response import build_response

        payload = (
            build_response(verdict)
            if verdict
            else {"eval_id": eval_id, "status": "complete"}
        )
        payload["evaluation_id"] = eval_id

        for attempt in range(3):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(url, json=payload, timeout=10.0)
                    resp.raise_for_status()
                    return
            except Exception:
                if attempt == 2:
                    logger.warning(
                        "Callback failed after 3 attempts: %s", url
                    )
