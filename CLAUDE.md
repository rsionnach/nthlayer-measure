# nthlayer-measure — Agent Context

Universal quality measurement engine for AI agent output. Evaluates agent output quality, tracks per-agent trends over rolling windows, detects degradation, self-calibrates its own judgment accuracy, and governs agent autonomy based on measured performance.

**Status: fully implemented — pipeline, store, trends, calibration (MAE + judgment SLOs + verdict-based), governance, degradation detector, OTel instrumentation, cost tracking, CLI subcommands, OpenSRM manifest integration, verdict integration (Phase 1), three adapters (webhook, GasTown, Devin), Prometheus SLO polling adapter with evaluate-once subcommand, and FastAPI HTTP API server.**

---

<!-- AUTO-MANAGED: build-commands -->
## Build Commands

- **Install dependencies:** `uv sync --extra dev --extra otel --no-sources`
- **Install with API extras:** `uv sync --extra dev --extra otel --extra api --no-sources`
- **Install nthlayer-learn (published):** `uv pip install "nthlayer-learn>=0.2.0"`
- **Run tests:** `uv run --no-sync pytest tests/ -v`
- **Run tests (CI flags):** `uv run --no-sync pytest tests/ -v --tb=short -x`
- **Run linting:** `uv run --no-sync ruff check src/ tests/ --ignore E501,B008,F841,B007,E402,E721,E722,B012,I001,F821,E741`
- **Run security scan (non-blocking):** `uv pip install pip-audit && uv run --no-sync pip-audit --progress-spinner off`
- **Run CLI:** `uv run nthlayer-measure serve | evaluate | status | calibrate | overrides | governance | evaluate-once | api-serve`
- **CI:** pushes/PRs to `main` or `develop`; matrix tests Python 3.11 and 3.12
<!-- END AUTO-MANAGED -->

---

## What This Is

The nthlayer-measure answers one question at production scale: which of my agents is producing good work, and which is silently degrading? It is framework-agnostic and model-agnostic. It works with any agent system via adapters, and the evaluation model is a configuration decision, not a hard dependency.

The nthlayer-measure is one component in the OpenSRM ecosystem (opensrm, nthlayer, nthlayer-correlate, nthlayer-respond) but is designed to stand alone. A team with no OpenSRM manifests can adopt the nthlayer-measure with a simple config file.

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

The adapter is the only integration point with external systems. Any agent system that implements the adapter interface can feed output into the nthlayer-measure. The core pipeline never knows or cares what produced the output.

Implemented adapters: webhook (generic HTTP POST), GasTown (polls bd quality-review-result wisps), Devin (polls Devin REST API for completed sessions). The webhook adapter is the default and works with anything.

**Adapter implementation notes:**
- **webhook**: Raw asyncio TCP server (no framework). Default bind address `127.0.0.1:8080` (not `0.0.0.0`). 64 KB header limit, 10 MB body limit, 1000-item bounded internal queue. POST-only; returns 400/413/431/503 on violations.
- **gastown**: Uses `asyncio.create_subprocess_exec` (not `shell=True`) to prevent injection. Queries `type:plugin-run` + `plugin:quality-review-result` wisps created in the last hour. Maps `worker` label to `agent_name`. 60s timeout on `proc.communicate()`.
- **devin**: Persistent lazy `httpx.AsyncClient` (one client per adapter instance, not per call). Polls `/v1/sessions`, fetches detail for completed/stopped/failed sessions. `_get_session` returns `None` on `HTTPError` and skips the yield — no exception propagation. Uses `structured_output` if present, falls back to `title`. Sets `agent_name = "devin:{session_id}"`.
- Both polling adapters (gastown, devin) use a `BoundedSeenSet` (from `adapters/_util.py`) capped at 10 000 entries (LRU eviction via `OrderedDict`) to prevent unbounded memory growth.

### Prometheus SLO Polling Adapter

`adapters/prometheus.py` — standalone Prometheus polling adapter. Does not require a `measure.yaml` config; used directly by the `evaluate-once` CLI subcommand.

**Core functions:**
- `load_specs(specs_dir)`: loads OpenSRM YAML specs from a directory, extracts SLO definitions, builds PromQL queries per SLO type. Availability target >1 is normalized to fraction (99.9 → 0.999). Judgment SLO names: `reversal_rate`, `high_confidence_failure`, `calibration`, `feedback_latency`.
- `evaluate_slos(prometheus_url, slos, verdict_store, hysteresis_threshold=3)`: async; evaluates all SLOs, applies breach semantics and hysteresis, returns `list[EvaluationResult]`.
- `query_prometheus(client, url, promql)`: async instant query; returns scalar float or `None` on failure/NaN (`val != val` check). Catches `httpx.HTTPError`, `ValueError`, `KeyError`, `IndexError`.
- `query_firing_alerts(client, url, service=None)`: async; queries `/api/v1/alerts`, returns list of dicts for alerts with `state=="firing"`; optional `service` label filter applied after fetch. Catches `httpx.HTTPError`, `ValueError`, `KeyError`.
- `count_consecutive_breaches(verdicts, service, slo_name)`: walks verdict list newest-first; matches on `v.subject.type=="evaluation"` AND `v.subject.ref==service` AND `custom["slo_name"]==slo_name`; counts consecutive windows where `current > target` (raw breach condition — NOT the final hysteresis-gated `breach` flag, which would create a catch-22), stops at first non-breach.

**PromQL queries (`_judgment_slo_query`):**
- `reversal_rate`: `sum(increase(gen_ai_overrides_total[w])) / sum(increase(gen_ai_decisions_total[w]))`
- `high_confidence_failure`: `sum(increase(gen_ai_overrides_hcf_total[w])) / sum(increase(gen_ai_decisions_total{confidence_bucket="high"}[w]))`
- `calibration`: `gen_ai_calibration_error{service=...}`
- `feedback_latency`: `gen_ai_feedback_latency_seconds{service=...}`

**Breach semantics:**
- `reversal_rate`, `high_confidence_failure`, `calibration`, `feedback_latency` (judgment): breach if `current > target`
- `availability` (traditional): breach if error budget ratio `< 0`
- `latency` (traditional): breach if `current > target / 1000` (target in ms, Prometheus value in seconds)
- all others: breach if `current < target`

**Hysteresis:**
- Judgment SLOs: breach only after `consecutive >= hysteresis_threshold` (default 3). Consecutive count is derived from recent verdicts in the verdict store via `VerdictFilter(producer_system="nthlayer-measure", subject_type="evaluation", limit=20)`.
- Traditional SLOs: breach immediately — Prometheus `for` duration handles hysteresis externally.

**Verdict shape from `evaluate-once`:**
- `subject.type="evaluation"`, `subject.ref=service`
- `judgment.action="flag"|"approve"`, `judgment.confidence=0.95` (traditional) or `0.85` (judgment)
- `metadata.custom`: slo_type, slo_name, target, current_value, breach, consecutive

### HTTP API Server

`api/` package — FastAPI HTTP API layer. Optional extra: `uv sync --extra api`. Requires `fastapi>=0.115` and `uvicorn[standard]>=0.34`. The `dev` extra includes `fastapi` for testing without uvicorn.

**`create_app(evaluator, store, tracker, dimensions, governance=None, verdict_store=None, approve_threshold=0.5, sync_timeout=30.0, max_workers=5, cors_origins=None) -> FastAPI`**
- CORS middleware enabled by default (`["*"]`); configurable via `cors_origins`.
- Components injected via closure — no FastAPI `Depends`.
- Lifespan context starts/stops `EvaluationQueue` workers on startup/shutdown.

**Routes:**

| Method | Path | Status | Purpose |
|--------|------|--------|---------|
| `GET` | `/api/v1/health` | 200 | Liveness check — `{"status": "ok"}` |
| `POST` | `/api/v1/evaluate` | 202 | Fire-and-forget; returns `evaluation_id`, `status`, `poll_url` |
| `POST` | `/api/v1/evaluate/sync` | 200/408 | Synchronous gate; returns verdict; on timeout returns 408 directly (does NOT re-submit to async queue) |
| `GET` | `/api/v1/evaluations/{eval_id}` | 200/404 | Poll for async result |
| `POST` | `/api/v1/override` | 200/404/409/422/503 | Override a verdict; 503 if no verdict store |
| `POST` | `/api/v1/confirm` | 200/404/409/422/503 | Confirm a verdict |
| `POST` | `/api/v1/resolve/batch` | 200/503 | Batch override/confirm; per-item error reporting in results array |
| `GET` | `/api/v1/agents/{agent_name}/accuracy` | 200/503 | Accuracy report (`?window=30d`); optional governance block |
| `GET` | `/api/v1/agents/{agent_name}/verdicts` | 200/503 | List verdicts (`?limit=20&status=...`) |
| `GET` | `/api/v1/governance/{agent_name}` | 200/503 | Governance status; 503 if not configured |

**`api/server.py` — `_parse_json(request)` helper:**
- Wraps `request.json()` in try/except; returns `JSONResponse(422, "Invalid JSON in request body")` on any parse failure. Used by all POST endpoints before field validation.

**`api/normalise.py` — `EvaluationRequest` + `normalise_input(body: dict)`:**
- Required fields: `agent`, `output`. Missing either raises `ValueError`.
- Optional with defaults: `task_id` (uuid4), `environment` ("production"), `context` (None), `service` (None), `callback_url` (None), `metadata` ({}).
- Extra fields silently ignored.

**`api/queue.py` — `EvaluationQueue`:**
- Async fire-and-forget processing pool. Default 5 workers.
- `submit(request) -> eval_id` returns immediately; `eval_id` format: `eval-{12 hex chars}`.
- `_results` is an `OrderedDict[str, dict]` capped at `MAX_RESULTS=10_000`; `submit()` evicts the oldest entry (`popitem(last=False)`) when the limit is exceeded — same LRU pattern as `BoundedSeenSet` in the polling adapters.
- Result states: `queued` → `evaluating` → `complete` | `error`; `not_found` for unknown ids.
- Creates verdicts on completion (mirrors `PipelineRouter` pattern, fail-open). Verdict creation failures logged at WARNING with `exc_info=True` (not silently swallowed).
- Fires `callback_url` via httpx POST with 3 retries if set on the request.

**`api/response.py` — `build_response(verdict, governance=None)` + `build_error_response(status_code, message, details=None)`:**
- Response keys: `verdict_id`, `action`, `score`, `confidence`, `dimensions`, `reasoning`, `risk_tier` (defaults to "standard"), optionally `governance`.
- `governance` key only present when `governance` arg is not `None`.

**`cmd_api_serve` wiring:**
- Builds store, evaluator, tracker from config.
- Verdict store wired if `config.verdict` present (sets `store._verdict_store`).
- Governance built only if `config.evaluator.model` is set.
- Launches via `uvicorn.run(app, host=..., port=...)`.

**Producer system note:** `EvaluationQueue._create_verdict` sets `producer.system="nthlayer-measure"` (not `"arbiter"`). The sync path in `server.py` reuses `queue._create_verdict` — same producer. Accuracy queries via `AccuracyFilter(producer_system="arbiter", ...)` will not match API-server verdicts; use `"nthlayer-measure"` when querying verdicts produced by the HTTP API.

**Window string parsing (`_parse_window`):** accepts `30d`, `7d`, `24h`, `4w`, `2m` → `datetime`; defaults to 30d on parse failure.

### Evaluation Pipeline

Receives normalised agent output from adapters, constructs an evaluation prompt with the output and declared quality dimensions, calls the configured evaluation model, parses and persists the resulting scores. The evaluation model is configured per-deployment — Claude, Gemini, or a local model. The transport layer is identical regardless of which model is used.

**ModelEvaluator details:**
- `_call_model` uses `nthlayer_common.llm.llm_call` via `asyncio.to_thread`, wrapped in `asyncio.wait_for` with a 120 s timeout. No direct Anthropic SDK — model routing is handled by the shared LLM wrapper.
- Token counts read from `result.input_tokens` / `result.output_tokens` (default 0 if absent); used for cost computation.
- Scores are clamped to [0.0, 1.0]. Markdown code fences are stripped before JSON parsing.
- Cost is computed from a hardcoded pricing table (returns `None` for unknown models):
  - `claude-sonnet-4-20250514`: $3.00 / $15.00 per MTok (input/output)
  - `claude-haiku-4-20250414`: $0.80 / $4.00 per MTok
  - `claude-opus-4-20250514`: $15.00 / $75.00 per MTok

### Score Store

Persists evaluation results with agent identity, timestamp, quality dimensions, confidence, and cost metadata. Implemented as SQLiteScoreStore with full CRUD for scores, overrides, and autonomy state. Schema is the contract — don't let storage implementation leak into the pipeline.

**SQLiteScoreStore implementation details:**
- All DB operations are guarded by a `threading.Lock`; async methods use `asyncio.to_thread` to avoid blocking the event loop.
- `save_override` validates that the `eval_id` exists before writing (raises `ValueError` on unknown id); calls `emit_override_event` inside the lock, then resolves linked verdict outside the lock.
- `get_overrides(since, limit=100, agent_name=None)`: optional `agent_name` filter via JOIN with evaluations table.
- `set_autonomy(agent_name, level, updated_by)`: upserts `agent_autonomy`, inserts `governance_log`; calls `emit_state_transition_event` outside the lock.
- Call `close()` to release the connection when done.
- Accepts optional `verdict_store: VerdictStoreBase | None = None`. When set: override triggers `verdict_store.resolve(verdict_id, "overridden", override={"by": corrector})` outside the lock.
- `set_verdict_id(eval_id, verdict_id)`: async; raises `ValueError` on unknown `eval_id`. Links evaluations row to verdict.
- `_migrate_verdict_id()`: idempotent `ALTER TABLE evaluations ADD COLUMN verdict_id TEXT`; swallows "duplicate column" `OperationalError`.

### Trend Tracker

Aggregates scores over configurable rolling windows per agent. Computes dimension averages, confidence mean, reversal rate, cost aggregation. No judgment logic here — this is arithmetic over stored scores.

### Degradation Detector

Watches per-agent trend metrics against declared SLO thresholds. Emits alerts when thresholds are breached (reversal rate, dimension scores, confidence). Threshold logic is deterministic — the model is not involved in deciding whether a threshold was crossed, only in evaluating the output that produced the score.

### Self-Calibration Loop

The nthlayer-measure monitors its own judgment quality. Human corrections (override events) feed into OverrideCalibration (MAE per dimension) and JudgmentSLOChecker (false accept rate, precision, recall, windowed compliance). When an agent has an OpenSRM manifest, compliance is checked against declared targets. OTel `gen_ai.calibration.report` events are emitted with all metrics.

**Signals tracked (two categories):**

Quality signals (per agent, per rolling window — pure arithmetic, no model):

| Signal | What it measures |
|--------|-----------------|
| Dimension averages | Mean score per quality dimension over the window |
| Confidence mean | Average confidence the evaluator model reported in its scores |
| Reversal rate | Fraction of evaluations later corrected by a human override |
| Cost per evaluation | Token spend per evaluation, broken down by agent |

Calibration signals (the nthlayer-measure judging itself against human corrections):

| Signal | Definition | Reference target |
|--------|-----------|-----------------|
| Reversal rate | Overridden evaluations / total evaluations | < 0.05 |
| False accept rate | Of outputs humans scored lower, how many did the nthlayer-measure score above threshold? | < 0.02 |
| Precision | Of outputs nthlayer-measure flagged low quality, what fraction did humans agree with? | > 0.90 |
| Recall | Of outputs humans corrected downward, what fraction did nthlayer-measure also flag? | > 0.85 |
| MAE | Mean absolute error between nthlayer-measure scores and human-corrected scores, per dimension | < 0.10 |

Reference targets are guidance, not enforced thresholds. Enforced targets come from OpenSRM manifests.

**JudgmentSLOChecker implementation details:**
- Metric computation is split into static helpers: `_compute_reversal_rate`, `_compute_false_accept_rate`, `_compute_precision`, `_compute_recall`, `_compute_mae`.
- `false_accept_rate`, `precision`, and `recall` require a `quality_threshold` from the manifest. When no manifest or no `quality_threshold` is declared, these metrics are `None` (fail open — no quality classification without operator-declared policy).
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
- Model call uses `nthlayer_common.llm.llm_call` via `asyncio.to_thread`, wrapped in `asyncio.wait_for` with a 60 s timeout. No direct Anthropic SDK.
- Fails open: if no model is configured, or the model call fails for any reason, no governance action is taken and the error is logged at WARNING level.

---

## Verdict Integration

Every evaluation creates a verdict via `PipelineRouter.run()`. Every human override resolves the linked verdict. System-wide accuracy is queryable via `VerdictCalibration` or `nthlayer-measure calibrate --verdict`.

**Integration points:**
- `PipelineRouter`: after `save_score()`, calls `verdict_create()` then `verdict_store.put()` then `store.set_verdict_id()`. Wrapped in try/except — fail open (logs WARNING, pipeline continues).
- `SQLiteScoreStore`: after `save_override()`, calls `verdict_store.resolve(verdict_id, "overridden", override={"by": corrector})` outside the threading lock.
- `VerdictCalibration` (`src/nthlayer_measure/calibration/verdict_calibration.py`): strangler fig alongside `JudgmentSLOChecker`. Queries `verdict_store.accuracy(AccuracyFilter(producer_system="arbiter", from_time=...))`. System-wide only — per-agent accuracy deferred to Phase 2+ (AccuracyFilter does not support filtering by subject.agent).

**Verdict shape produced by nthlayer-measure:**
- `subject.type`: always `"agent_output"` | `subject.ref`: task_id | `subject.agent`: agent_name | `subject.summary`: `"Evaluation of {agent_name}: {task_id}"`
- `judgment.action`: `"approve"` if avg dimension score >= `approve_threshold` else `"reject"`
- `judgment.confidence`: score.confidence | `judgment.score`: mean of all dimension scores | `judgment.dimensions`: per-dimension dict
- `judgment.reasoning`: semicolon-separated `"key: value"` string from reasoning dict
- `producer.system`: always `"arbiter"` | `producer.model`: evaluator model name
- `metadata.cost_currency`: score.cost_usd
- `DEFAULT_APPROVE_THRESHOLD = 0.5` (configurable via `PipelineRouter(approve_threshold=...)`)

---

## OpenSRM Integration

When an OpenSRM manifest is present, the nthlayer-measure reads judgment SLO thresholds from it:

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

The nthlayer-measure uses the OpenSRM OTel semantic conventions for AI decision telemetry:

- `gen_ai.decision.evaluated` — emitted on every evaluation (attrs: eval_id, agent_name, task_id, confidence, evaluator_model, dimension_count, cost_usd, alert_count)
- `gen_ai.override.applied` — emitted when a human corrects an evaluation (attrs: eval_id, dimension, original_score, corrected_score, corrector)
- `gen_ai.calibration.report` — emitted on every `JudgmentSLOChecker.check()` call (attrs: agent_name, window_days, reversal_rate, mae, false_accept_rate, precision, recall, reversal_rate_compliant)
- `gen_ai.agent.state.changed` — emitted on governance state transitions (attrs: agent_name, from_level, to_level, triggered_by)

All four are no-ops if `opentelemetry` is not installed (`telemetry.py` catches `ImportError` at module load).

These feed into NthLayer-generated dashboards and nthlayer-correlate correlation. Emit them consistently — they are the integration surface with the rest of the ecosystem.

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

# Optional — enables verdict integration. Absent = no verdict ops (fully backwards-compat).
verdict:
  store:
    path: verdicts.db

# Optional — trigger downstream chain on breach. Both sections default to disabled.
trigger:
  correlate:
    enabled: false
    args: {}
  respond:
    enabled: false
    args: {}
```

**TriggerConfig** (`config.py`): `correlate_enabled`, `correlate_args`, `respond_enabled`, `respond_args`. Parsed from `trigger.correlate` and `trigger.respond` YAML blocks. Defaults to all disabled. Used by `evaluate-once` to invoke `nthlayer-correlate correlate` and/or `nthlayer-respond respond` when SLO breaches are detected.

**Config validation:** `load_config` raises `ValueError` if any top-level section (e.g. `evaluator`, `store`) is not a YAML mapping, or if any entry in `agents:` is not a mapping or is missing the required `name` field. Default dimensions when `dimensions:` is omitted: `["correctness", "completeness", "safety"]`.

**Demo:** `demo-arbiter.yaml` is a ready-to-run config. Databases (`demo-arbiter.db`, `demo-verdicts.db`) are gitignored and generated by running the demo — not committed. Run with `ANTHROPIC_API_KEY=... arbiter -c demo-arbiter.yaml serve`, then POST agent output to `http://127.0.0.1:8080`.

---

## CLI Subcommands

`nthlayer-measure` is the entry point (`python -m nthlayer_measure` or installed script). All subcommands accept `-c/--config <path>` (default: `arbiter.yaml`). When no subcommand is given, `serve` runs by default.

| Subcommand | Purpose |
|------------|---------|
| `serve` | Start the full evaluation pipeline (adapter → evaluator → store → governance). Only the first agent in `agents:` is wired; warns to stderr if more than one is configured. |
| `evaluate [file] --agent-name A [--task-id T] [--output-type T]` | One-shot evaluation from positional file path or stdin; prints JSON result |
| `status <agent_name> [--window-days N]` | Print trend window + autonomy level as JSON (agent_name is positional) |
| `calibrate [--agent A] [--window-days N] [--verdict]` | MAE report (all agents), SLO compliance report (per agent with manifest), or verdict-based accuracy report (`--verdict`; requires `verdict:` section in config). `--verdict` JSON fields: `producer`, `total`, `total_resolved`, `confirmation_rate`, `override_rate`, `partial_rate`, `pending_rate`, `mean_confidence_on_confirmed`, `mean_confidence_on_overridden`. |
| `overrides list [--agent A] [--days N]` | List recent human overrides as JSON |
| `overrides create <eval_id> --corrector P --dimension name=score [...]` | Create a human override for an evaluation (repeatable `--dimension`). When `verdict:` is configured, wires `verdict_store` so the override resolves the linked verdict as "overridden". |
| `governance show <agent_name>` | Print current autonomy level (agent_name is positional) |
| `governance restore <agent_name> <level> --approver P` | Restore autonomy; agent_name and level are positional, --approver is required (safety ratchet) |
| `evaluate-once <specs_dir> --prometheus-url U --verdict-store PATH [--hysteresis N]` | One-shot Prometheus SLO evaluation: loads OpenSRM specs from dir, queries Prometheus, writes verdicts to verdict store, exits. Exits 2 if any breach detected. Verdict confidence: 0.95 (traditional SLO) or 0.85 (judgment SLO). When `trigger.correlate.enabled=true` in config, `_trigger_chain()` queries verdict store for most recent `nthlayer-measure/evaluation` verdict and invokes `nthlayer-correlate correlate --trigger-verdict <id>`; passes `--respond-args <json>` if respond also enabled. No measure.yaml required for core evaluation; config needed for trigger chain. |
| `api-serve [--host H] [--port P] [--sync-timeout S] [--workers W]` | Start the FastAPI HTTP API server (requires `api` extra). Defaults: host `127.0.0.1`, port `8080`. OpenAPI docs at `http://{host}:{port}/docs`. Reads evaluator/store/governance/verdict config from `measure.yaml`. |

---

## Testing

`tests/test_webhook.py` — webhook adapter HTTP contract tests. Uses `port=0` for random port assignment (`server.sockets[0].getsockname()[1]`). Coverage:
- Valid POST → 200 (`{"status": "ok"}`); item retrievable from `adapter._queue`
- GET → 405; missing required fields → 400; invalid JSON → 400
- Body > 10 MB (via `Content-Length`) → 413; headers > 64 KB → 431; queue full (1000 items) → 503

`tests/test_prometheus.py` — Prometheus polling adapter tests. Uses `MemoryStore` from nthlayer_learn as verdict_store fixture. Coverage:
- `load_specs`: parses 3 SLOs from sample spec (availability, reversal_rate, latency), classifies judgment vs traditional, normalizes availability target (99.9 → 0.999), builds correct PromQL (gen_ai_overrides_total / gen_ai_decisions_total for reversal_rate), handles empty dir.
- `query_firing_alerts`: returns only `state=="firing"` alerts (not pending); optional `service=` filter applied after fetch returns only matching service label.
- `query_prometheus`: returns float value, returns `None` on empty results, returns `None` on NaN response.
- `count_consecutive_breaches`: counts consecutive windows where `current > target` from newest verdict, stops at first non-breach, returns 0 when newest is not a raw breach.
- `evaluate_slos`: healthy → no breach; judgment breach below hysteresis threshold (consecutive=1, breach=False); judgment breach at threshold (3 consecutive, breach=True); traditional SLO breaches immediately without hysteresis; recovery (value returns healthy) resets consecutive to 0; SLO with no Prometheus data (query returns None) is skipped — not included in results.

`tests/test_api_normalise.py` — `normalise_input` unit tests. Coverage: all fields populated, minimal input fills defaults (uuid task_id, "production" environment), missing `agent` raises `ValueError`, missing `output` raises `ValueError`, extra fields silently ignored, returns `EvaluationRequest` type.

`tests/test_api_queue.py` — `EvaluationQueue` async tests. Uses `pytest-asyncio`. Coverage: `submit` returns `eval-` prefixed id, result is `complete` after processing, `not_found` for unknown id, `error` status on evaluator exception, verdict created and stored when `verdict_store` provided, `callback_url` fires httpx POST on completion.

`tests/test_api_response.py` — `build_response` / `build_error_response` tests. Coverage: all response keys present, dimensions defaults to `{}` when absent, governance block added only when passed, error response with/without `details`.

`tests/test_api_server.py` — FastAPI server contract tests via `TestClient`. Coverage: health endpoint, async evaluate (202/queued), sync evaluate returns verdict, sync timeout returns 408 directly (no re-submission to queue), poll for nonexistent returns 404, poll after submit, override/confirm/batch (success, missing verdict 404, already-resolved 409, missing fields 422), accuracy and verdicts query endpoints, governance status and 503 when not configured, override 503 when no verdict store, malformed JSON body → 422 with "Invalid JSON" (`test_evaluate_invalid_json_body`), sync eval without verdict store → 200 score-based response with eval_id/action/dimensions/confidence (`test_evaluate_sync_without_verdict_store`).

`tests/test_verdict_integration.py` — Phase 1 integration test suite. Covers:
- `TestVerdictConfig`: config loading with/without `verdict:` section; `VerdictConfig` default `store_path="verdicts.db"`.
- `TestSchemaMigration`: `verdict_id` column present after init, NULL by default, settable via `set_verdict_id`, raises `ValueError` on unknown `eval_id`, migration is idempotent.
- `TestVerdictEmission`: verdict shape, `verdict_id` written to evaluations row, approve/reject boundary at `DEFAULT_APPROVE_THRESHOLD=0.5`, custom threshold, graceful no-op when `verdict_store=None`, reasoning as semicolon-separated string.
- `TestOverrideResolution`: override resolves linked verdict; pre-integration data (no `verdict_id`) handled without error; `SQLiteScoreStore` without `verdict_store` is backward-compatible.
- `TestVerdictCalibration`: `VerdictCalibration.check()` accuracy rates, empty store returns zeros, `window_days` respected.
- `TestCalibrateVerdictFlag`: end-to-end `cmd_calibrate --verdict`; error path when config missing `verdict:` section.

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
| [opensrm](../opensrm/) | Shared manifest spec |
| [nthlayer-learn](../nthlayer-learn/) | Data primitive — nthlayer-measure evaluation output becomes a verdict; self-calibration queries verdict accuracy |
| [nthlayer-measure](../nthlayer-measure/) | This repo — quality measurement + governance |
| [nthlayer](../nthlayer/) | Generates monitoring infrastructure from manifests |
| [nthlayer-correlate](../nthlayer-correlate/) | Signal correlation and situational awareness |
| [nthlayer-respond](../nthlayer-respond/) | Multi-agent incident response |

Each component works independently. Composition happens through shared OpenSRM manifests and OTel conventions.

---

## Prior Art

The core concept was validated as the Guardian, a Deacon plugin inside GasTown that scores per-worker output quality in the merge pipeline (PR #2263, merged). The nthlayer-measure extracts that pattern into a universal, framework-agnostic tool.
