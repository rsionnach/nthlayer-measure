"""Evaluator protocol and implementation — the boundary between transport and judgment."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, replace
from typing import Protocol

from nthlayer_measure.types import AgentOutput, QualityScore


class Evaluator(Protocol):
    """Evaluates agent output across quality dimensions.

    The evaluator is the boundary where transport hands off to judgment.
    It constructs the prompt, calls the model, and parses the response.
    It never interprets quality itself — that's the model's job (ZFC).
    """

    async def evaluate(self, output: AgentOutput, dimensions: list[str]) -> QualityScore: ...


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
        self._client = None

    def _get_client(self):
        """Lazy-init the Anthropic client."""
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic()
        return self._client

    def build_prompt(self, output: AgentOutput, dimensions: list[str]) -> str:
        """Construct the evaluation prompt. This IS the deliverable — prompt engineering."""
        dimensions_block = "\n".join(f"- {d}" for d in dimensions)
        return f"""You are an evaluation judge. Score the following agent output on each dimension.

## Agent Output
- Agent: {output.agent_name}
- Task: {output.task_id}
- Type: {output.output_type}

### Content
<agent_output>
{output.output_content}
</agent_output>

## Dimensions to Score
{dimensions_block}

## Instructions
For each dimension, provide:
1. A score from 0.0 to 1.0
2. Brief reasoning for your score

Also provide an overall confidence score (0.0 to 1.0) representing how confident you are in your evaluation.

## Response Format
Respond with valid JSON only:
{{
  "dimensions": {{
    "<dimension_name>": {{"score": <float>, "reasoning": "<string>"}},
    ...
  }},
  "confidence": <float>
}}"""

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
        """Call the Anthropic API and return text + token counts."""
        client = self._get_client()
        response = await asyncio.wait_for(
            client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=self._timeout,
        )
        if not response.content:
            raise ValueError("Model returned empty content")
        text = response.content[0].text
        return _ModelResponse(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    async def evaluate(self, output: AgentOutput, dimensions: list[str]) -> QualityScore:
        prompt = self.build_prompt(output, dimensions)
        model_response = await self._call_model(prompt)
        score = self.parse_response(model_response.text, output)
        cost = _compute_cost(self._model, model_response.input_tokens, model_response.output_tokens)
        if cost is not None:
            score = replace(score, cost_usd=cost)
        return score
