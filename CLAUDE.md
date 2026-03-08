# Arbiter — Agent Context

Universal quality measurement engine for AI agent output. Evaluates agent output quality, tracks per-agent trends over rolling windows, detects degradation, self-calibrates its own judgment accuracy, and governs agent autonomy based on measured performance.

**Status: fully implemented — pipeline, store, trends, calibration (MAE + judgment SLOs), governance, degradation detector, OTel instrumentation, cost tracking, CLI subcommands, OpenSRM manifest integration, and three adapters (webhook, GasTown, Devin).**

---

## What This Is

The Arbiter answers one question at production scale: which of my agents is producing good work, and which is silently degrading? It is framework-agnostic and model-agnostic. It works with any agent system via adapters, and the evaluation model is a configuration decision, not a hard dependency.

The Arbiter is one component in the OpenSRM ecosystem (opensrm, nthlayer, sitrep, mayday) but is designed to stand alone. A team with no OpenSRM manifests can adopt the Arbiter with a simple config file.

---

## Core Design Principle: ZFC

**Zero Framework Cognition** — draw a hard line between transport and judgment.

**Transport (code handles this):**
- Receiving agent output via adapters
- Routing output to the evaluation model
- Persisting quality scores to storage
- Computing trend aggregations over rolling windows
- Sending alerts when degradation is detected
- Adjusting agent autonomy configuration based on governance decisions

**Judgment (model handles this):**
- Evaluating whether output is correct, complete, and safe
- Deciding whether a quality trend represents genuine degradation or normal variance
- Understanding that 0.79 on a documentation task is acceptable while 0.79 on a security review is alarming

If a decision requires context, nuance, or interpretation — it belongs to the model. If it is mechanical, deterministic, or structural — it belongs to the code. Never put judgment logic in code. Never put transport logic in prompts.

---

## Architecture

```
Agent Output ──▶ Adapter ──▶ Evaluation Pipeline ──▶ Score Store
                                     │
                                     ├── Trend Tracker (rolling windows)
                                     ├── Degradation Detector
                                     ├── Self-Calibration Loop
                                     ├── Cost Tracker
                                     └── Governance Engine
```

### Adapter Interface

The adapter is the only integration point with external systems. Any agent system that implements the adapter interface can feed output into the Arbiter. The core pipeline never knows or cares what produced the output.

Implemented adapters: webhook (generic HTTP POST), GasTown (polls bd quality-review-result wisps), Devin (polls Devin REST API for completed sessions). The webhook adapter is the default and works with anything.

**Adapter implementation notes:**
- **webhook**: Raw asyncio TCP server (no framework). Default bind address `127.0.0.1:8080` (not `0.0.0.0`). 64 KB header limit, 10 MB body limit, 1000-item bounded internal queue. POST-only; returns 400/413/431/503 on violations.
- **gastown**: Uses `asyncio.create_subprocess_exec` (not `shell=True`) to prevent injection. Queries `type:plugin-run` + `plugin:quality-review-result` wisps created in the last hour. Maps `worker` label to `agent_name`.
- **devin**: Persistent lazy `httpx.AsyncClient` (one client per adapter instance, not per call). Polls `/v1/sessions`, fetches detail for completed/stopped/failed sessions. `_get_session` returns `None` on `HTTPError` and skips the yield — no exception propagation. Uses `structured_output` if present, falls back to `title`. Sets `agent_name = "devin:{session_id}"`.
- Both polling adapters (gastown, devin) use a `_BoundedSeenSet` capped at 10 000 entries (LRU eviction via `OrderedDict`) to prevent unbounded memory growth.

### Evaluation Pipeline

Receives normalised agent output from adapters, constructs an evaluation prompt with the output and declared quality dimensions, calls the configured evaluation model, parses and persists the resulting scores. The evaluation model is configured per-deployment — Claude, Gemini, or a local model. The transport layer is identical regardless of which model is used.

**ModelEvaluator details:**
- Lazy-init `anthropic.AsyncAnthropic` client; uses `asyncio.wait_for` with a 120 s timeout.
- Scores are clamped to [0.0, 1.0]. Markdown code fences are stripped before JSON parsing.
- Cost is computed from a hardcoded pricing table (returns `None` for unknown models):
  - `claude-sonnet-4-20250514`: $3.00 / $15.00 per MTok (input/output)
  - `claude-haiku-4-20250414`: $0.80 / $4.00 per MTok
  - `claude-opus-4-20250514`: $15.00 / $75.00 per MTok

### Score Store

Persists evaluation results with agent identity, timestamp, quality dimensions, confidence, and cost metadata. Implemented as SQLiteScoreStore with full CRUD for scores, overrides, and autonomy state. Schema is the contract — don't let storage implementation leak into the pipeline.

**SQLiteScoreStore implementation details:**
- All DB operations are guarded by a `threading.Lock`; async methods use `asyncio.to_thread` to avoid blocking the event loop.
- `save_override` validates that the `eval_id` exists before writing (raises `ValueError` on unknown id).
- Call `close()` to release the connection when done.

### Trend Tracker

Aggregates scores over configurable rolling windows per agent. Computes dimension averages, confidence mean, reversal rate, cost aggregation. No judgment logic here — this is arithmetic over stored scores.

### Degradation Detector

Watches per-agent trend metrics against declared SLO thresholds. Emits alerts when thresholds are breached (reversal rate, dimension scores, confidence). Threshold logic is deterministic — the model is not involved in deciding whether a threshold was crossed, only in evaluating the output that produced the score.

### Self-Calibration Loop

The Arbiter monitors its own judgment quality. Human corrections (override events) feed into OverrideCalibration (MAE per dimension) and JudgmentSLOChecker (false accept rate, precision, recall, windowed compliance). When an agent has an OpenSRM manifest, compliance is checked against declared targets. OTel `gen_ai.calibration.report` events are emitted with all metrics.

**Signals tracked (two categories):**

Quality signals (per agent, per rolling window — pure arithmetic, no model):

| Signal | What it measures |
|--------|-----------------|
| Dimension averages | Mean score per quality dimension over the window |
| Confidence mean | Average confidence the evaluator model reported in its scores |
| Reversal rate | Fraction of evaluations later corrected by a human override |
| Cost per evaluation | Token spend per evaluation, broken down by agent |

Calibration signals (the Arbiter judging itself against human corrections):

| Signal | Definition | Reference target |
|--------|-----------|-----------------|
| Reversal rate | Overridden evaluations / total evaluations | < 0.05 |
| False accept rate | Of outputs humans scored lower, how many did the Arbiter score above threshold? | < 0.02 |
| Precision | Of outputs Arbiter flagged low quality, what fraction did humans agree with? | > 0.90 |
| Recall | Of outputs humans corrected downward, what fraction did Arbiter also flag? | > 0.85 |
| MAE | Mean absolute error between Arbiter scores and human-corrected scores, per dimension | < 0.10 |

Reference targets are guidance, not enforced thresholds. Enforced targets come from OpenSRM manifests.

**JudgmentSLOChecker implementation details:**
- Metric computation is split into static helpers: `_compute_reversal_rate`, `_compute_false_accept_rate`, `_compute_precision`, `_compute_recall`, `_compute_mae`.
- After computing the report, `check()` calls `emit_calibration_report_event` directly — the OTel event is fired as part of every `check()` call, not only on manifest violations.

### Governance Engine

Implemented as ErrorBudgetGovernance. On each `check_agent` call, fetches the agent's trend window and calls the configured Anthropic model with trend data and operator context; the model decides (ZFC) whether autonomy should be reduced. The following behaviors are the intended design target:

| Trigger | Action | Implemented |
|---------|--------|-------------|
| Model judges degradation significant | Reduce autonomy one step | Yes |
| Sustained good performance | Propose autonomy increase (requires human approval) | No |
| Calibration drift detected | Flag for retraining or prompt adjustment | No |
| Multiple agents degrading simultaneously | Escalate, suggest system-wide review | No |

**The one-way safety ratchet is a hard constraint:** the Governance Engine can always reduce agent autonomy. It can never increase autonomy without explicit human approval. This is not a policy decision — it is a design constraint. Do not build any code path that autonomously increases agent permissions.

**ErrorBudgetGovernance implementation details:**
- Reduction ladder: `FULL → SUPERVISED → ADVISORY_ONLY → SUSPENDED` (SUSPENDED is terminal).
- `restore_autonomy(agent, level, approver)` raises `ValueError` if `approver` is an empty string.
- `build_governance_prompt` passes `error_budget_threshold` as operator context ("the operator considers this concerning") — it is not a hard code-level trigger. The model reads the threshold and decides whether degradation is significant enough to act on.
- Model call uses `asyncio.wait_for` with a 60 s timeout; lazy `_get_client()` init.
- Fails open: if no model is configured, or the model call fails for any reason, no governance action is taken and the error is logged at WARNING level.

---

## OpenSRM Integration

When an OpenSRM manifest is present, the Arbiter reads judgment SLO thresholds from it:

```yaml
apiVersion: opensrm/v1
kind: ServiceReliabilityManifest
metadata:
  name: code-reviewer-agent
  tier: critical
spec:
  type: ai-gate
  slos:
    judgment:
      reversal:
        rate:
          target: 0.05
          window: 30d
      high_confidence_failure:
        target: 0.02
        confidence_threshold: 0.9
```

OpenSRM integration is additive — a plain `arbiter.yaml` config works without it. Never make the manifest a hard dependency.

---

## OTel Conventions

The Arbiter uses the OpenSRM OTel semantic conventions for AI decision telemetry:

- `gen_ai.decision.*` — emitted on every evaluation
- `gen_ai.override.*` — emitted when a human corrects an evaluation
- `gen_ai.agent.state.*` — emitted on governance state transitions

These feed into NthLayer-generated dashboards and SitRep correlation. Emit them consistently — they are the integration surface with the rest of the ecosystem.

---

## Configuration

```yaml
# arbiter.yaml
evaluator:
  model: claude-sonnet-4-20250514
  max_tokens: 4096
  temperature: 0.0

store:
  backend: sqlite
  path: arbiter.db

governance:
  error_budget_window_days: 7
  error_budget_threshold: 0.5

dimensions:
  - correctness
  - completeness
  - safety

detection:
  max_reversal_rate: 0.3
  min_confidence: 0.5
  min_dimension_scores:
    correctness: 0.6

agents:
  - name: code-reviewer
    adapter: webhook
    manifest: manifests/code-reviewer.yaml
  - name: gastown-worker
    adapter: gastown
    adapter_config:
      rig_name: wyvern
      poll_interval: 60
  - name: devin-worker
    adapter: devin
    adapter_config:
      api_key_env: DEVIN_API_KEY
      poll_interval: 30
```

**Config validation:** `load_config` raises `ValueError` if any top-level section (e.g. `evaluator`, `store`) is not a YAML mapping, or if any entry in `agents:` is not a mapping or is missing the required `name` field. Default dimensions when `dimensions:` is omitted: `["correctness", "completeness", "safety"]`.

---

## CLI Subcommands

`arbiter` is the entry point (`python -m arbiter` or installed script). All subcommands accept `-c/--config <path>` (default: `arbiter.yaml`). When no subcommand is given, `serve` runs by default.

| Subcommand | Purpose |
|------------|---------|
| `serve` | Start the full evaluation pipeline (adapter → evaluator → store → governance). Only the first agent in `agents:` is wired; warns to stderr if more than one is configured. |
| `evaluate [file] --agent-name A [--task-id T] [--output-type T]` | One-shot evaluation from positional file path or stdin; prints JSON result |
| `status <agent_name> [--window-days N]` | Print trend window + autonomy level as JSON (agent_name is positional) |
| `calibrate [--agent A] [--window-days N]` | MAE report (all agents) or SLO compliance report (per agent with manifest) |
| `overrides list [--agent A] [--days N]` | List recent human overrides as JSON (`list` is a required sub-subcommand) |
| `governance show <agent_name>` | Print current autonomy level (agent_name is positional) |
| `governance restore <agent_name> <level> --approver P` | Restore autonomy; agent_name and level are positional, --approver is required (safety ratchet) |

---

## What Not to Build

- Do not build agent-framework-specific logic into the core pipeline. That belongs in adapters.
- Do not hardcode quality thresholds. They come from config or OpenSRM manifests.
- Do not build autonomous autonomy-increase paths. Governance can only reduce autonomy without human approval.
- Do not put judgment logic (context-sensitive decisions) in code. Route them to the model.
- Do not couple storage implementation to the pipeline. The score schema is the contract.

---

## Ecosystem

| Component | Role |
|-----------|------|
| [opensrm](https://github.com/rsionnach/opensrm) | Shared manifest spec |
| [arbiter](https://github.com/rsionnach/arbiter) | This repo — quality measurement + governance |
| [nthlayer](https://github.com/rsionnach/nthlayer) | Generates monitoring infrastructure from manifests |
| [sitrep](https://github.com/rsionnach/sitrep) | Signal correlation and situational awareness |
| [mayday](https://github.com/rsionnach/mayday) | Multi-agent incident response |

Each component works independently. Composition happens through shared OpenSRM manifests and OTel conventions.

---

## Prior Art

The core concept was validated as the Guardian, a Deacon plugin inside GasTown that scores per-worker output quality in the merge pipeline (PR #2263, merged). The Arbiter extracts that pattern into a universal, framework-agnostic tool.
