# Zero Framework Cognition (ZFC)

## Core Tenet of the OpenSRM Ecosystem

**Transport is code. Judgment is model.**

Code handles movement: receiving inputs, routing them, persisting results, exposing APIs, sending alerts. Code never decides whether something is good, bad, important, risky, or correct. That is judgment, and judgment belongs to the model.

This principle governs every architectural decision across the OpenSRM ecosystem.


## Origin

ZFC was articulated by Steve Yegge as a foundational design principle for GasTown: "Go code should never make judgment calls. Go is transport, not intelligence. Claude makes the decisions."

The principle emerged from a practical observation. When you encode judgment into compiled code (scoring functions, threshold comparisons, classification logic), you get decisions that can't adapt, can't reason about edge cases, and can't improve without a code change. A hardcoded `if score < 0.8 { status = "WARN" }` treats a 0.79 on a documentation typo the same as a 0.79 on broken authentication logic. A model understands the difference. Code never will.


## The Distinction

Transport is deterministic transformation with no ambiguity about what the right answer is:

- Receiving a diff from a webhook and passing it to a review function
- Persisting a quality score to a database or state file
- Generating a Prometheus recording rule from a declared SLO target of `p99: 200ms`
- Sending an alert when the model says to alert
- Routing a message between agents via a mail queue
- Rendering a dashboard from stored metrics
- Validating that a YAML manifest conforms to a JSON schema

Judgment involves interpretation, evaluation, or any decision where context changes the right answer:

- Reading a diff and deciding whether the code is correct
- Scoring quality across dimensions (correctness, clarity, risk)
- Deciding whether a pattern of declining scores constitutes a real problem or normal variance
- Determining which services in a codebase are critical and what SLO targets they need
- Evaluating whether an incident is resolved or still degraded
- Correlating a quality drop with a recent change and deciding if the change caused it
- Recommending an action (nudge, escalate, park) based on agent behaviour patterns


## Policy Enforcement vs Quality Judgment

Not all threshold comparisons are judgment calls. ZFC distinguishes between two uses of thresholds in code:

**Policy Enforcement (compliant — code handles this):**
- Budget caps: "stop spending after $100"
- Rate limits: "no more than 10 requests per minute"
- Confidence gates: "don't proceed if confidence is below 0.7"
- Approval gates: "don't execute without human sign-off"

These are mechanical stop/go decisions with no ambiguity. The operator declares a hard limit, and code enforces it. The code is not interpreting quality — it is enforcing a constraint.

**Quality Judgment (forbidden — model handles this):**
- "Is this score good enough?" — depends on context
- "Is this agent degrading?" — depends on trend interpretation
- "Should we alert on this pattern?" — depends on what the pattern means
- Classifying outputs as "low quality" vs "acceptable" based on a numeric threshold

The difference: policy enforcement gates are binary (proceed/don't proceed) and context-independent. Quality judgments classify or rank and the right answer changes based on what you're looking at. A budget cap of $100 means the same thing regardless of the task. A quality threshold of 0.5 means something completely different for a documentation review vs a security audit.

When a threshold is used as operator context for a model ("the operator considers rates above 20% concerning"), that is ZFC-compliant. When the same threshold is used by code to classify quality (`if avg < 0.5: label = "low_quality"`), that is a violation.


## Practical Implications

### Config as context, not as triggers

Traditional approach: config defines thresholds, code compares values against thresholds, code decides the outcome.

ZFC approach: config defines context and preferences, the model receives that context alongside the data, the model decides the outcome. A rejection rate threshold of 0.20 in config means "the operator considers 20% rejection concerning" not "trigger WARN at exactly 0.20."

This does not apply to policy enforcement. A budget cap of $100 in config means exactly "stop at $100." That is a mechanical gate, not a quality assessment.

### Fail open on model unavailability

If the model is unavailable, transport continues. Data is received, persisted, and routed. Judgment pauses. The system degrades to "no quality opinion" rather than "wrong quality opinion."

### Self-calibration is native

Because judgment comes from the model, every judgment is inherently auditable. The model said "this is good" and a human later corrected it. That's a measurable signal. Under ZFC, self-calibration (judgment SLOs on the judging agent itself) is a natural consequence of the architecture rather than an afterthought bolted on.

### Model-agnostic by design

Transport code doesn't care which model provides the judgment. Swap Claude for Gemini, GPT, or a local model and the transport layer is unchanged. The quality of judgment changes, which is itself measurable through the self-calibration loop. This makes every tool in the ecosystem model-portable without code changes.


## ZFC Categories (from Steve Yegge's AGENTS.md)

**ZFC-Compliant (code):**
- IO and Plumbing — read/write files, parse JSON, persist to stores, watch events
- Structural Safety Checks — schema validation, required fields, path traversal prevention, timeouts
- Policy Enforcement — budget caps, rate limits, confidence gates, approval gates
- Mechanical Transforms — parameter substitution, compilation, formatting model output
- State Management — lifecycle tracking, progress monitoring, escalation policy execution
- Typed Error Handling — SDK error classes, no message parsing

**ZFC-Violations (model only):**
- Ranking/Scoring/Selection — any algorithm choosing among alternatives based on heuristics
- Plan/Composition/Scheduling — decisions about dependencies, ordering, parallelization, retry policies
- Semantic Analysis — inferring complexity, scope, file dependencies, "what should be done next"
- Heuristic Classification — keyword-based routing, fallback decision trees, domain-specific rules
- Quality Judgment — opinionated validation beyond structural safety

**The correct flow:** Gather raw context (IO only) → Call AI for decisions → Validate structure → Execute mechanically.


## What ZFC Is Not

ZFC is not "put an LLM in every code path." Most code in the ecosystem is and should remain pure transport. ZFC applies specifically to the decision points where context matters and the right answer depends on interpretation.

ZFC is also not "models are always right." The self-calibration loop exists precisely because models make bad judgments. The point is that bad model judgments are improvable (through better prompts, better context, better models) while bad hardcoded judgments require code changes and redeployment.


## Summary

Every component in the OpenSRM ecosystem asks two questions about each function it performs:

1. Is there exactly one right answer given the inputs? That's transport. Write it in code.
2. Does the right answer depend on context, interpretation, or evaluation? That's judgment. Send it to the model.

Transport is code. Judgment is model. No exceptions.
