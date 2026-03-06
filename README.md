# Arbiter

**Universal quality measurement engine for AI agent output.**

[![Status: Implemented](https://img.shields.io/badge/Status-Implemented-green?style=for-the-badge)](https://github.com/rsionnach/arbiter)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-green?style=for-the-badge)](LICENSE)

If you're running multiple AI agents in production, you're asking the same question every team asks at scale: "which of my agents is producing good work, and which is silently producing garbage?" The Arbiter answers that question. Point it at your agents, and it tells you which ones are reliable, which ones are degrading, and which ones need to be reined in before they cause damage.

The Arbiter evaluates AI agent output quality, tracks per-agent quality trends over rolling windows, detects quality degradation, measures its own accuracy through self-calibration, and governs agent autonomy based on measured performance. It works with any agent system (not just a specific framework) and any model provider (swap Claude for Gemini or a local model and the transport doesn't change, only the judgment quality changes, which is itself measurable).

This project is in the architecture phase. The design is documented below, and implementation has not yet started.

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
arbiter -c arbiter.yaml
```

---

## Architecture

### ZFC: Transport vs Judgment

The Arbiter is built on [Zero Framework Cognition](ZFC.md), which draws a hard line between transport and judgment.

**Transport (code):** Receiving agent output from adapters, routing it to the evaluation model, persisting quality scores to storage, computing trend aggregations over rolling windows, sending alerts when degradation is detected, adjusting agent autonomy configuration based on governance decisions.

**Judgment (model):** Evaluating whether agent output is correct, complete, and safe. Deciding whether a quality trend represents genuine degradation or normal variance. Assessing whether a score of 0.79 on a documentation task is acceptable while 0.79 on a security review is alarming. The model understands context in ways that hardcoded thresholds never will.

This separation means the Arbiter is model-agnostic by design. Swap the evaluation model and the transport layer is unchanged. The quality of judgment changes, and that change is itself measurable through the self-calibration loop.

---

## Self-Calibration

The Arbiter doesn't just evaluate other agents, it evaluates itself. When the Arbiter scores agent output as 'good' and a human later corrects it, that's a measurable signal. The Arbiter tracks its own judgment SLOs:

- **False accept rate:** How often does the Arbiter approve output that humans later reject?
- **Precision:** When the Arbiter flags something as low quality, how often are humans in agreement?
- **Recall:** Of the outputs that humans flag as problematic, what percentage did the Arbiter catch?

Every evaluation the Arbiter produces emits a `gen_ai.decision.*` OTel event. Every human correction emits a `gen_ai.override.*` event. These feed into the Arbiter's own judgment SLO, which the Arbiter itself monitors (and which humans can review through the same dashboards that track any other agent).

---

## Governance

The Arbiter subsumes the role of a reliability governor. Rather than having two separate systems that watch agent quality, the Arbiter both measures quality and acts on those measurements.

### How governance works

The Arbiter watches judgment SLO error budgets for every agent it monitors. When an agent's quality degrades beyond its declared thresholds, the Arbiter takes governance actions:

| Trigger | Action |
|---------|--------|
| Reversal rate exceeds SLO target | Increase human review threshold for that agent |
| Error budget exhausted | Reduce agent to advisory-only mode (suggest, don't act) |
| Sustained good performance above threshold | Propose autonomy increase (requires human approval) |
| Calibration drift detected | Flag agent for retraining or prompt adjustment |
| Multiple agents degrading simultaneously | Escalate to human operators, suggest system-wide review |

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

- **GasTown adapter:** Connects to GasTown's merge pipeline, receives per-worker output for quality evaluation
- **Generic webhook adapter:** Accepts agent output via HTTP webhook with a simple JSON schema, works with any system that can make HTTP calls
- **Devin adapter:** Connects to Devin's task output for evaluation (planned)

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

The Arbiter core is implemented: evaluation pipeline, SQLite score store, trend tracking, override-based calibration, and error-budget governance with a one-way safety ratchet. The model evaluator constructs prompts and parses responses but requires an SDK dependency (e.g. `anthropic`) to be wired for live model calls. Judgment SLOs (reversal rate targets, windowed compliance) are a planned next layer on top of the existing calibration infrastructure.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## License

Apache License 2.0. See [LICENSE](LICENSE).
