"""Tests for ModelEvaluator — prompt construction + response parsing."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nthlayer_measure.pipeline.evaluator import ModelEvaluator, _ModelResponse, _compute_cost
from nthlayer_measure.types import AgentOutput


@pytest.fixture
def evaluator():
    return ModelEvaluator(model="test-model", max_tokens=2048)


@pytest.fixture
def sample_output():
    return AgentOutput(
        agent_name="agent-a",
        task_id="task-1",
        output_content="def hello(): return 'world'",
        output_type="code",
    )


def test_build_prompt_contains_dimensions(evaluator, sample_output):
    prompt = evaluator.build_prompt(sample_output, ["correctness", "style"])
    assert "correctness" in prompt
    assert "style" in prompt
    assert "agent-a" in prompt
    assert "task-1" in prompt
    assert "def hello()" in prompt


def test_build_prompt_contains_response_format(evaluator, sample_output):
    prompt = evaluator.build_prompt(sample_output, ["correctness"])
    assert '"dimensions"' in prompt
    assert '"confidence"' in prompt
    assert "JSON" in prompt


def test_parse_response_valid_json(evaluator, sample_output):
    response = json.dumps({
        "dimensions": {
            "correctness": {"score": 0.9, "reasoning": "Mostly correct"},
            "style": {"score": 0.7, "reasoning": "Could improve"},
        },
        "confidence": 0.85,
    })

    score = evaluator.parse_response(response, sample_output)
    assert score.agent_name == "agent-a"
    assert score.task_id == "task-1"
    assert score.dimensions["correctness"] == pytest.approx(0.9)
    assert score.dimensions["style"] == pytest.approx(0.7)
    assert score.reasoning["correctness"] == "Mostly correct"
    assert score.confidence == pytest.approx(0.85)
    assert score.evaluator_model == "test-model"


def test_parse_response_strips_code_fences(evaluator, sample_output):
    response = "```json\n" + json.dumps({
        "dimensions": {"x": {"score": 0.5, "reasoning": "r"}},
        "confidence": 0.6,
    }) + "\n```"

    score = evaluator.parse_response(response, sample_output)
    assert score.dimensions["x"] == pytest.approx(0.5)


def test_parse_response_invalid_json(evaluator, sample_output):
    with pytest.raises(json.JSONDecodeError):
        evaluator.parse_response("not json", sample_output)


def test_compute_cost_known_model():
    cost = _compute_cost("claude-sonnet-4-20250514", 1000, 500)
    # (1000 * 3.0 + 500 * 15.0) / 1_000_000 = (3000 + 7500) / 1_000_000 = 0.0105
    assert cost == pytest.approx(0.0105)


def test_compute_cost_unknown_model():
    cost = _compute_cost("unknown-model", 1000, 500)
    assert cost is None


def test_parse_response_clamps_out_of_range_scores(evaluator, sample_output):
    response = json.dumps({
        "dimensions": {
            "correctness": {"score": 1.5, "reasoning": "Over"},
            "style": {"score": -0.3, "reasoning": "Under"},
        },
        "confidence": 2.0,
    })
    score = evaluator.parse_response(response, sample_output)
    assert score.dimensions["correctness"] == pytest.approx(1.0)
    assert score.dimensions["style"] == pytest.approx(0.0)
    assert score.confidence == pytest.approx(1.0)


def test_parse_response_strips_only_outer_fences(evaluator, sample_output):
    """Code fences within JSON values should not be stripped."""
    inner = json.dumps({
        "dimensions": {"x": {"score": 0.5, "reasoning": "r"}},
        "confidence": 0.6,
    })
    # Wrap with fences
    response = f"```json\n{inner}\n```"
    score = evaluator.parse_response(response, sample_output)
    assert score.dimensions["x"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_evaluate_with_mock_client(evaluator, sample_output):
    response_json = json.dumps({
        "dimensions": {
            "correctness": {"score": 0.9, "reasoning": "Good"},
        },
        "confidence": 0.85,
    })

    mock_response = _ModelResponse(text=response_json, input_tokens=100, output_tokens=50)

    with patch.object(evaluator, "_call_model", new_callable=AsyncMock, return_value=mock_response):
        score = await evaluator.evaluate(sample_output, ["correctness"])

    assert score.agent_name == "agent-a"
    assert score.dimensions["correctness"] == pytest.approx(0.9)
    assert score.confidence == pytest.approx(0.85)
