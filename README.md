# Arbiter

**Universal quality measurement engine for AI agent output.**

[![Status: Implemented](https://img.shields.io/badge/Status-Implemented-green?style=for-the-badge)](https://github.com/rsionnach/arbiter)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-green?style=for-the-badge)](LICENSE)

If you're running multiple AI agents in production, you're asking the same question every team asks at scale: "which of my agents is producing good work, and which is silently producing garbage?" The Arbiter answers that question. Point it at your agents, and it tells you which ones are reliable, which ones are degrading, and which ones need to be reined in before they cause damage.

The Arbiter evaluates AI agent output quality, tracks per-agent quality trends over rolling windows, detects quality degradation, measures its own accuracy through self-calibration, and governs agent autonomy based on measured performance. It works with any agent system (not just a specific framework) and any model provider (swap Claude for Gemini or a local model and the transport doesn't change, only the judgment quality changes, which is itself measurable).

The core is fully implemented: evaluation pipeline, score store, trend tracking, degradation detection, self-calibration with judgment SLOs, governance with a one-way safety ratchet, OTel instrumentation, cost tracking, CLI subcommands, OpenSRM manifest integration, and three adapters (webhook, GasTown, Devin).

---

## How It Works

```
Agent Output ──▶ Arbiter ──▶ Quality Scores + Trends + Alerts
                   │
                   ├── Per-agent quality tracking (rolling windows)
                   ├── Degradation detection
                   ├── Self-calibration (judgment SLOs on itself)
                   ├── Cost-per-quality tracking
                   └── Governance (autonomy adjustments)
```

The Arbiter receives agent output (via adapters for different agent systems), routes it to a model for quality evaluation, persists the scores, tracks trends over time, and detects when quality degrades. The entire pipeline follows [Zero Framework Cognition](ZFC.md): the code handles receiving, routing, persisting, and alerting (transport), while the model handles the actual quality evaluation (judgment).

---

## Quick Start

```yaml
# arbiter.yaml
evaluator:
  model: claude-sonnet-4-20250514
  max_tokens: 4096

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

agents:
  - name: code-reviewer
    adapter: webhook
  - name: doc-writer
    adapter: webhook
```

```bash
pip install -e .

# Start the evaluation pipeline (listens for agent output via webhook)
arbiter serve

# One-shot evaluation of a file
arbiter evaluate output.txt --agent-name code-reviewer

# Check agent status (trend window + autonomy level)
arbiter status code-reviewer

# Run calibration report (how accurate is the Arbiter's own judgment?)
arbiter calibrate --agent code-reviewer
```

---

## CLI

All subcommands accept `-c/--config <path>` (default: `arbiter.yaml`).

```bash
# Start the pipeline — adapter listens, evaluates incoming output, persists scores,
# runs governance checks. Default when no subcommand is given.
arbiter serve

# Evaluate a single file or stdin. Prints JSON with scores, confidence, cost.
arbiter evaluate output.txt --agent-name my-agent --task-id pr-1234
echo "some output" | arbiter evaluate --agent-name my-agent

# Agent trend window: dimension averages, reversal rate, confidence, cost, autonomy level
arbiter status my-agent --window-days 14

# Calibration: how well does the Arbiter agree with human corrections?
# Without --agent: MAE report across all agents
# With --agent: full judgment SLO compliance report (uses OpenSRM manifest if available)
arbiter calibrate --window-days 30
arbiter calibrate --agent code-reviewer --window-days 30

# List recent human overrides (corrections to Arbiter scores)
arbiter overrides list --days 7 --agent code-reviewer

# Governance: check current autonomy level
arbiter governance show my-agent

# Governance: restore autonomy (requires human approver — safety ratchet)
arbiter governance restore my-agent full --approver admin@example.com
```

### Sample output

```bash
$ arbiter status code-reviewer
{
  "agent_name": "code-reviewer",
  "window_days": 7,
  "dimension_averages": {"correctness": 0.87, "completeness": 0.82, "safety": 0.95},
  "evaluation_count": 42,
  "confidence_mean": 0.84,
  "reversal_rate": 0.024,
  "total_cost_usd": 0.63,
  "avg_cost_per_eval": 0.015,
  "autonomy": "full"
}
```

---

## Architecture

### ZFC: Transport vs Judgment

The Arbiter is built on [Zero Framework Cognition](ZFC.md), which draws a hard line between transport and judgment.

**Transport (code):** Receiving agent output from adapters, routing it to the evaluation model, persisting quality scores to storage, computing trend aggregations over rolling windows, sending alerts when degradation is detected, adjusting agent autonomy configuration based on governance decisions.

**Judgment (model):** Evaluating whether agent output is correct, complete, and safe. Deciding whether a quality trend represents genuine degradation or normal variance. Assessing whether a score of 0.79 on a documentation task is acceptable while 0.79 on a security review is alarming. The model understands context in ways that hardcoded thresholds never will.

This separation means the Arbiter is model-agnostic by design. Swap the evaluation model and the transport layer is unchanged. The quality of judgment changes, and that change is itself measurable through the self-calibration loop.

---

## Signals & Judgment SLOs

The Arbiter tracks two categories of signals: **quality signals** (about the agents it monitors) and **calibration signals** (about its own judgment accuracy).

### Quality signals (per agent, per rolling window)

These are computed from stored evaluation scores. No model involvement — pure arithmetic (ZFC: transport).

| Signal | What it measures |
|--------|-----------------|
| **Dimension averages** | Mean score per quality dimension (e.g. correctness: 0.87, safety: 0.95) over the window |
| **Confidence mean** | Average confidence the evaluator model reported in its own scores |
| **Reversal rate** | Fraction of evaluations that were later corrected by a human override. A reversal rate of 0.05 means 5% of the Arbiter's judgments were wrong enough for a human to step in. |
| **Cost per evaluation** | Token spend per evaluation, broken down by agent |

### Calibration signals (the Arbiter judging itself)

When a human submits an override (correcting an Arbiter score), that override becomes ground truth. The Arbiter measures how well its judgments align with human corrections:

| Signal | Definition | Good value |
|--------|-----------|------------|
| **Reversal rate** | Overridden evaluations / total evaluations | < 0.05 (5%) |
| **False accept rate** | Of outputs where humans scored lower than the Arbiter, how many did the Arbiter score above the passing threshold? Measures how often the Arbiter lets bad work through. | < 0.02 (2%) |
| **Precision** | Of outputs the Arbiter flagged as low quality, what fraction did humans agree with (no upward override)? High precision = few false alarms. | > 0.90 |
| **Recall** | Of outputs humans corrected downward, what fraction did the Arbiter also flag as low quality? High recall = the Arbiter catches what humans catch. | > 0.85 |
| **MAE** | Mean absolute error between original Arbiter scores and human-corrected scores, per dimension. | < 0.10 |

### OpenSRM judgment SLO targets

When an agent has an [OpenSRM manifest](#integration-with-opensrm), the Arbiter checks calibration signals against declared targets:

```yaml
slos:
  judgment:
    reversal:
      rate:
        target: 0.05      # reversal rate must stay below 5%
        window: 30d
    high_confidence_failure:
      target: 0.02         # false accept rate below 2%
      confidence_threshold: 0.9
```

Without a manifest, the Arbiter still computes all signals — it just doesn't have compliance targets to check against.

### Running a calibration report

```bash
$ arbiter calibrate --agent code-reviewer --window-days 30
{
  "agent_name": "code-reviewer",
  "window_days": 30,
  "reversal_rate": 0.024,
  "reversal_rate_target": 0.05,
  "reversal_rate_compliant": true,
  "false_accept_rate": 0.01,
  "precision": 0.94,
  "recall": 0.88,
  "mae": 0.07,
  "total_evaluations": 142,
  "total_overrides": 3
}
```

Every `check()` call emits a `gen_ai.calibration.report` OTel event with all metrics, feeding into NthLayer dashboards and SitRep correlation.

---

## Governance

The Arbiter subsumes the role of a reliability governor. Rather than having two separate systems that watch agent quality, the Arbiter both measures quality and acts on those measurements.

### How governance works

On each evaluation, the governance engine fetches the agent's trend window and asks the configured model whether autonomy should be reduced. The operator's error budget threshold is passed as context ("the operator considers scores below 0.5 concerning"), but the model makes the judgment call — not a hardcoded comparison (ZFC).

| Trigger | Action | Status |
|---------|--------|--------|
| Model judges degradation significant | Reduce autonomy one step (full → supervised → advisory-only → suspended) | Implemented |
| Sustained good performance | Propose autonomy increase (requires human approval) | Planned |
| Calibration drift detected | Flag for retraining or prompt adjustment | Planned |
| Multiple agents degrading simultaneously | Escalate, suggest system-wide review | Planned |

### The one-way safety ratchet

This is a critical design constraint: the Arbiter can always reduce agent autonomy (the safe direction) but can never increase it without human approval. Automation can always be constrained, never self-expanded. An agent that starts producing unreliable output gets reined in automatically. An agent that has been performing well for months still needs a human to say "yes, give it more autonomy."

---

## Cost Tracking

The Arbiter tracks cost per agent per task alongside quality scores, because reliability decisions have cost implications and cost pressures affect reliability.

- **Token spend, API calls, and compute cost** are measured and correlated with quality for each agent
- **Cost-per-quality-unit** answers the question "how much does it cost to produce good output from this agent?" and becomes a first-class metric alongside reversal rate and calibration
- **Cost budgets** can be declared in OpenSRM manifests alongside SLO targets, giving operators a unified view of quality and efficiency
- An agent that's expensive and low-quality gets constrained faster than one that's cheap and low-quality

This matters because teams routinely cut review depth or use cheaper models to save on token costs, which degrades quality, which causes incidents. Making cost visible alongside quality helps operators make informed tradeoffs rather than optimising one dimension at the expense of the other.

---

## Integration with OpenSRM

The Arbiter reads judgment SLO thresholds from [OpenSRM](https://github.com/rsionnach/opensrm) manifests when they're available. An agent's manifest declares its quality targets:

```yaml
# agent.reliability.yaml
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

The Arbiter also works with simple configuration files for teams that don't use OpenSRM. The manifest integration is additive, not required.

---

## Adapters

The Arbiter connects to different agent systems through adapters. Each adapter translates a specific system's output format into the Arbiter's evaluation pipeline.

- **GasTown adapter:** Polls bd for quality-review-result wisps from the Refinery merge pipeline, converts to AgentOutput
- **Generic webhook adapter:** Accepts agent output via HTTP webhook with a simple JSON schema, works with any system that can make HTTP calls
- **Devin adapter:** Polls Devin REST API (`GET /v1/sessions`) for completed sessions and evaluates their output

Building a custom adapter is straightforward: implement the input interface to receive agent output, and the Arbiter handles evaluation, scoring, trending, and governance from there.

---

## OpenSRM Ecosystem

The Arbiter is one component in the OpenSRM ecosystem. Each component solves a complete problem independently, and they compose when used together through shared OpenSRM manifests and OTel telemetry conventions.

```
                        ┌─────────────────────────┐
                        │     OpenSRM Manifest     │
                        │  (the shared contract)   │
                        └────────────┬────────────┘
                                     │
                    reads            │           reads
               ┌─────────────┬──────┴──────┬─────────────┐
               ▼             ▼             ▼             ▼
         ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
         │>>ARBITER<│ │ NthLayer │ │  SitRep  │ │  Mayday  │
         │          │ │          │ │          │ │          │
         │ quality  │ │ generate │ │correlate │ │ incident │
         │+govern   │ │ monitoring│ │ signals  │ │ response │
         │+cost     │ │          │ │          │ │          │
         └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
              │             │             │             │
              └──────┬──────┴──────┬──────┘             │
                     ▼             ▼                    ▼
              ┌────────────────────────────┐  ┌──────────────┐
              │  Streaming / Queue Layer   │  │  Consumes    │
              │  (Kafka / NATS / etc)      │  │  all three   │
              └──────────┬─────────────────┘  └──────┬───────┘
                         ▼                           │
              ┌────────────────────────┐             │
              │   OTel Collector /     │             │
              │   Prometheus / etc     │             │
              └────────────────────────┘             │
                                                     │
              ┌──────────────────────────────────────┘
              │  Learning loop (post-incident):
              │  Mayday findings → manifest updates
              │  → NthLayer regenerates → Arbiter
              │  refines → SitRep improves
              └──────────────────────────────────────▶ OpenSRM
```

**How the Arbiter fits in:**

- **Quality scores** that the Arbiter produces flow as OTel metrics into the streaming layer, where [NthLayer](https://github.com/rsionnach/nthlayer) generates dashboards for them, [SitRep](https://github.com/rsionnach/sitrep) correlates them with other signals (like recent deployments or model version changes), and [Mayday](https://github.com/rsionnach/mayday) uses them during incident response
- **Governance decisions** from the Arbiter adjust agent autonomy across the ecosystem, including Mayday's incident response agents and SitRep's correlation agent
- **Cost tracking data** feeds into NthLayer-generated dashboards so operators see quality and cost together
- The Arbiter consumes **change events** (via the OpenSRM change event schema) to contextualise quality shifts, distinguishing between quality drops caused by model version changes and genuine agent degradation

Each component works alone. Someone who just needs agent quality measurement adopts the Arbiter without needing NthLayer, SitRep, or Mayday.

| Component | What it does | Link |
|-----------|-------------|------|
| **OpenSRM** | Specification for declaring service reliability requirements | [opensrm](https://github.com/rsionnach/opensrm) |
| **Arbiter** | Quality measurement and governance for AI agents (this repo) | [arbiter](https://github.com/rsionnach/arbiter) |
| **NthLayer** | Generate monitoring infrastructure from manifests | [nthlayer](https://github.com/rsionnach/nthlayer) |
| **SitRep** | Situational awareness through signal correlation | [sitrep](https://github.com/rsionnach/sitrep) |
| **Mayday** | Multi-agent incident response | [mayday](https://github.com/rsionnach/mayday) |

---

## Prior Art

The Arbiter concept was proven inside GasTown as the Guardian, a Deacon plugin that scores per-worker output quality in the merge pipeline ([PR #2263](https://github.com/nicholasgastown/nicholasgastown/pull/2263), merged by Steve Yegge). The Guardian demonstrated that automated quality measurement with self-calibration works in practice at production scale. The Arbiter extracts this pattern into a universal tool that any multi-agent system can use, regardless of the underlying agent framework or model provider.

---

## Status

The Arbiter is fully implemented:

- **Pipeline**: Evaluation pipeline with live Anthropic SDK calls, cost tracking, and OTel instrumentation
- **Store**: SQLite score store with scores, overrides, autonomy state, and governance log
- **Trends**: Rolling window aggregation with dimension averages, reversal rate, confidence mean, cost stats
- **Detection**: Threshold-based degradation detector with alerts for reversal rate, dimension scores, and confidence
- **Calibration**: MAE-based override calibration + judgment SLO checker (false accept rate, precision, recall, windowed compliance)
- **Governance**: Error-budget governance with one-way safety ratchet
- **Adapters**: Webhook (generic), GasTown (bd wisps), Devin (REST API)
- **CLI**: Subcommands — `serve`, `evaluate`, `status`, `calibrate`, `overrides list`, `governance show/restore`
- **OpenSRM**: Manifest loader for judgment SLO thresholds

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## License

Apache License 2.0. See [LICENSE](LICENSE).
