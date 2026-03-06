# Arbiter — Agent Context

Universal quality measurement engine for AI agent output. Evaluates agent output quality, tracks per-agent trends over rolling windows, detects degradation, self-calibrates its own judgment accuracy, and governs agent autonomy based on measured performance.

**Status: architecture phase — implementation not yet started.**

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

Planned adapters: webhook (generic, any system that can POST JSON), GasTown, Devin. The webhook adapter is the default and works with anything.

### Evaluation Pipeline

Receives normalised agent output from adapters, constructs an evaluation prompt with the output and declared quality dimensions, calls the configured evaluation model, parses and persists the resulting scores. The evaluation model is configured per-deployment — Claude, Gemini, or a local model. The transport layer is identical regardless of which model is used.

### Score Store

Persists evaluation results with agent identity, timestamp, quality dimensions, confidence, and cost metadata. The backing store should be simple and self-contained for v1 (SQLite is fine). Schema is the contract — don't let storage implementation leak into the pipeline.

### Trend Tracker

Aggregates scores over configurable rolling windows per agent. Computes reversal rate, false accept rate, precision, and recall across windows. No judgment logic here — this is arithmetic over stored scores.

### Degradation Detector

Watches per-agent trend metrics against declared SLO thresholds. Emits alerts when thresholds are breached. Threshold logic is deterministic — the model is not involved in deciding whether a threshold was crossed, only in evaluating the output that produced the score.

### Self-Calibration Loop

The Arbiter monitors its own judgment quality using the same pipeline it uses for other agents. Human corrections (override events) feed back into the Arbiter's own metrics: false accept rate, precision, recall. Every evaluation emits a `gen_ai.decision.*` OTel event. Every human correction emits a `gen_ai.override.*` event. The Arbiter has its own judgment SLO which humans can review through the same dashboards as any other agent.

### Governance Engine

Watches judgment SLO error budgets. Takes governance actions when agents degrade:

| Trigger | Action |
|---------|--------|
| Reversal rate exceeds SLO target | Increase human review threshold for that agent |
| Error budget exhausted | Reduce agent to advisory-only mode |
| Sustained good performance | Propose autonomy increase (requires human approval) |
| Calibration drift detected | Flag for retraining or prompt adjustment |
| Multiple agents degrading simultaneously | Escalate, suggest system-wide review |

**The one-way safety ratchet is a hard constraint:** the Governance Engine can always reduce agent autonomy. It can never increase autonomy without explicit human approval. This is not a policy decision — it is a design constraint. Do not build any code path that autonomously increases agent permissions.

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
  dimensions: [correctness, completeness, safety]

agents:
  - name: code-reviewer
    source: webhook
    judgment_slo:
      reversal_rate:
        target: 0.05
        window: 30d
```

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
