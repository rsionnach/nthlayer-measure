"""Evaluator protocol and implementation — the boundary between transport and judgment."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from nthlayer_common.prompts import load_prompt, render_user_prompt

_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "evaluator.yaml"

from nthlayer_measure.types import AgentOutput, QualityScore


class Evaluator(Protocol):
    """Evaluates agent output across quality dimensions.

    The evaluator is the boundary where transport hands off to judgment.
    It constructs the prompt, calls the model, and parses the response.
    It never interprets quality itself — that's the model's job (ZFC).
    """

    async def evaluate(self, output: AgentOutput, dimensions: list[str], model: str | None = None) -> QualityScore: ...


@dataclass(frozen=True)
class _ModelResponse:
    """Internal response container from model call."""

    text: str
    input_tokens: int
    output_tokens: int


# Known model pricing per million tokens (input, output) in USD
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-haiku-4-20250414": (0.80, 4.0),
    "claude-opus-4-20250514": (15.0, 75.0),
}


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Compute cost in USD from token counts. Returns None for unknown models."""
    pricing = _MODEL_PRICING.get(model)
    if pricing is None:
        return None
    input_price, output_price = pricing
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class ModelEvaluator:
    """Evaluator that delegates quality judgment to a language model.

    Constructs evaluation prompts, sends to model, parses structured
    responses back into QualityScore. All judgment lives in the prompt,
    not in this code.
    """

    def __init__(self, model: str, max_tokens: int = 4096, timeout: float = 120.0) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._timeout = timeout

    def build_prompt(self, output: AgentOutput, dimensions: list[str]) -> str:
        """Construct the evaluation prompt from YAML template."""
        spec = load_prompt(_PROMPT_PATH)
        dimensions_block = "\n".join(f"- {d}" for d in dimensions)
        return render_user_prompt(
            spec.user_template,
            agent_name=output.agent_name,
            task_id=output.task_id,
            output_type=output.output_type,
            output_content=output.output_content,
            dimensions_block=dimensions_block,
        )

    def parse_response(self, raw: str, output: AgentOutput) -> QualityScore:
        """Parse model response JSON into a QualityScore. Pure transport."""
        from nthlayer_measure._parsing import strip_markdown_fences

        text = strip_markdown_fences(raw)
        data = json.loads(text)
        dimensions: dict[str, float] = {}
        reasoning: dict[str, str] = {}

        for dim_name, dim_data in data["dimensions"].items():
            dimensions[dim_name] = _clamp(float(dim_data["score"]))
            if "reasoning" in dim_data:
                reasoning[dim_name] = dim_data["reasoning"]

        return QualityScore(
            eval_id=str(uuid.uuid4()),
            agent_name=output.agent_name,
            task_id=output.task_id,
            dimensions=dimensions,
            reasoning=reasoning,
            confidence=_clamp(float(data["confidence"])),
            evaluator_model=self._model,
        )

    async def _call_model(self, prompt: str) -> _ModelResponse:
        """Call the LLM via the shared nthlayer-common wrapper."""
        from nthlayer_common.llm import llm_call

        result = await asyncio.wait_for(
            asyncio.to_thread(
                llm_call,
                system="",
                user=prompt,
                model=self._model,
                max_tokens=self._max_tokens,
                timeout=int(self._timeout),
            ),
            timeout=self._timeout,
        )
        if not result.text:
            raise ValueError("Model returned empty content")
        return _ModelResponse(
            text=result.text,
            input_tokens=result.input_tokens or 0,
            output_tokens=result.output_tokens or 0,
        )

    async def evaluate(self, output: AgentOutput, dimensions: list[str], model: str | None = None) -> QualityScore:
        effective_model = model or self._model
        prompt = self.build_prompt(output, dimensions)
        # Temporarily use override model for the call
        original_model = self._model
        if model:
            self._model = effective_model
        try:
            model_response = await self._call_model(prompt)
        finally:
            self._model = original_model
        score = self.parse_response(model_response.text, output)
        if model:
            score = replace(score, evaluator_model=effective_model)
        cost = _compute_cost(effective_model, model_response.input_tokens, model_response.output_tokens)
        if cost is not None:
            score = replace(score, cost_usd=cost)
        return score
