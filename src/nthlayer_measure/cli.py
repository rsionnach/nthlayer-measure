"""CLI entry point for nthlayer-measure — subcommands for serve, evaluate, status, calibrate, overrides, governance."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from nthlayer_measure.config import MeasureConfig, load_config
from nthlayer_measure.types import AutonomyLevel


def _load_config(args: argparse.Namespace) -> MeasureConfig:
    config_path = getattr(args, "config", None) or Path("measure.yaml")
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    return load_config(config_path)


def _build_store(config: MeasureConfig) -> "SQLiteScoreStore":
    from nthlayer_measure.store.sqlite import SQLiteScoreStore

    return SQLiteScoreStore(config.store.path)


def _build_tracker(store: "SQLiteScoreStore") -> "StoreTrendTracker":
    from nthlayer_measure.trends.tracker import StoreTrendTracker

    return StoreTrendTracker(store)


def _build_evaluator(config: MeasureConfig) -> "ModelEvaluator":
    from nthlayer_measure.pipeline.evaluator import ModelEvaluator

    return ModelEvaluator(
        model=config.evaluator.model,
        max_tokens=config.evaluator.max_tokens,
    )


def _build_adapter(config: MeasureConfig):
    """Build adapter from config. Supports webhook, gastown, devin.

    Currently only the first agent config is used for adapter construction.
    """
    agents = config.agents
    if not agents:
        from nthlayer_measure.adapters.webhook import WebhookAdapter

        return WebhookAdapter()

    if len(agents) > 1:
        print(
            f"Warning: {len(agents)} agents configured but only the first is used by 'serve'",
            file=sys.stderr,
        )

    agent = agents[0]
    ac = agent.adapter_config

    if agent.adapter == "gastown":
        from nthlayer_measure.adapters.gastown import GasTownAdapter

        return GasTownAdapter(
            rig_name=ac.get("rig_name", ""),
            poll_interval=ac.get("poll_interval", 60.0),
            bd_path=ac.get("bd_path", "bd"),
        )
    elif agent.adapter == "devin":
        from nthlayer_measure.adapters.devin import DevinAdapter
        import os

        api_key_env = ac.get("api_key_env", "DEVIN_API_KEY")
        return DevinAdapter(
            api_key=os.environ.get(api_key_env, ""),
            poll_interval=ac.get("poll_interval", 30.0),
            base_url=ac.get("base_url", "https://api.devin.ai"),
        )
    else:
        from nthlayer_measure.adapters.webhook import WebhookAdapter

        return WebhookAdapter(
            host=ac.get("host", "127.0.0.1"),
            port=ac.get("port", 8080),
        )


def _build_pipeline(config: MeasureConfig):
    from nthlayer_measure.detection.detector import SLOThresholds, ThresholdDetector
    from nthlayer_measure.governance.engine import ErrorBudgetGovernance
    from nthlayer_measure.pipeline.router import PipelineRouter
    from nthlayer_measure.store.sqlite import SQLiteScoreStore

    # Build verdict store if configured
    verdict_store = None
    if config.verdict is not None:
        from nthlayer_learn import SQLiteVerdictStore
        verdict_store = SQLiteVerdictStore(config.verdict.store_path)

    # Share the same verdict store between score store (for override resolution)
    # and router (for verdict creation)
    store = SQLiteScoreStore(config.store.path, verdict_store=verdict_store)
    tracker = _build_tracker(store)
    evaluator = _build_evaluator(config)
    governance = ErrorBudgetGovernance(
        store=store,
        tracker=tracker,
        window_days=config.governance.error_budget_window_days,
        threshold=config.governance.error_budget_threshold,
        model=config.evaluator.model,
    )
    thresholds = SLOThresholds(
        max_reversal_rate=config.detection.max_reversal_rate,
        min_dimension_scores=config.detection.min_dimension_scores,
        min_confidence=config.detection.min_confidence,
    )
    detector = ThresholdDetector(thresholds)
    adapter = _build_adapter(config)

    return PipelineRouter(
        adapter=adapter,
        evaluator=evaluator,
        store=store,
        tracker=tracker,
        dimensions=config.dimensions,
        governance=governance,
        detector=detector,
        verdict_store=verdict_store,
    )


# --- Subcommand handlers ---


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the evaluation pipeline (default behavior)."""
    config = _load_config(args)
    router = _build_pipeline(config)
    asyncio.run(router.run())


def cmd_evaluate(args: argparse.Namespace) -> None:
    """One-shot evaluation of a file or stdin."""
    from nthlayer_measure.types import AgentOutput

    config = _load_config(args)
    store = _build_store(config)
    evaluator = _build_evaluator(config)

    if args.file:
        content = Path(args.file).read_text()
    else:
        content = sys.stdin.read()

    output = AgentOutput(
        agent_name=args.agent_name,
        task_id=args.task_id,
        output_content=content,
        output_type=args.output_type,
    )

    async def _run():
        score = await evaluator.evaluate(output, config.dimensions)
        await store.save_score(score)
        return score

    score = asyncio.run(_run())
    result = {
        "eval_id": score.eval_id,
        "agent_name": score.agent_name,
        "task_id": score.task_id,
        "dimensions": score.dimensions,
        "confidence": score.confidence,
        "cost_usd": score.cost_usd,
    }
    print(json.dumps(result, indent=2))


def cmd_status(args: argparse.Namespace) -> None:
    """Show agent trend window + autonomy level."""
    config = _load_config(args)
    store = _build_store(config)
    tracker = _build_tracker(store)

    async def _run():
        window = await tracker.compute_window(args.agent_name, args.window_days)
        autonomy = await store.get_autonomy(args.agent_name)
        return window, autonomy

    window, autonomy = asyncio.run(_run())
    result = {
        "agent_name": window.agent_name,
        "window_days": window.window_days,
        "dimension_averages": window.dimension_averages,
        "evaluation_count": window.evaluation_count,
        "confidence_mean": window.confidence_mean,
        "reversal_rate": window.reversal_rate,
        "total_cost_usd": window.total_cost_usd,
        "avg_cost_per_eval": window.avg_cost_per_eval,
        "autonomy": autonomy or "full",
    }
    print(json.dumps(result, indent=2))


def cmd_calibrate(args: argparse.Namespace) -> None:
    """Run calibration report."""
    config = _load_config(args)

    if getattr(args, "verdict", False):
        # Verdict-based calibration (system-wide)
        if config.verdict is None:
            print(
                "Error: --verdict requires a 'verdict' section in measure.yaml",
                file=sys.stderr,
            )
            sys.exit(1)

        from nthlayer_learn import SQLiteVerdictStore
        from nthlayer_measure.calibration.verdict_calibration import VerdictCalibration

        verdict_store = SQLiteVerdictStore(config.verdict.store_path)
        cal = VerdictCalibration(verdict_store)

        async def _run():
            return await cal.check(window_days=args.window_days)

        report = asyncio.run(_run())
        verdict_store.close()
        result = {
            "producer": report.producer,
            "total": report.total,
            "total_resolved": report.total_resolved,
            "confirmation_rate": report.confirmation_rate,
            "override_rate": report.override_rate,
            "partial_rate": report.partial_rate,
            "pending_rate": report.pending_rate,
            "mean_confidence_on_confirmed": report.mean_confidence_on_confirmed,
            "mean_confidence_on_overridden": report.mean_confidence_on_overridden,
        }
        print(json.dumps(result, indent=2))
        return

    store = _build_store(config)

    async def _run():
        if args.agent:
            from nthlayer_measure.calibration.slos import JudgmentSLOChecker
            from nthlayer_measure.manifest import load_manifest

            slo = None
            for ac in config.agents:
                if ac.name == args.agent and ac.manifest:
                    slo = load_manifest(Path(ac.manifest))
                    break

            checker = JudgmentSLOChecker(store, slo=slo)
            report = await checker.check(args.agent, window_days=args.window_days)
            return asdict(report)
        else:
            from nthlayer_measure.calibration.loop import OverrideCalibration

            cal = OverrideCalibration(store)
            report = await cal.calibrate(window_days=args.window_days)
            return asdict(report)

    result = asyncio.run(_run())
    print(json.dumps(result, indent=2))


def cmd_overrides_create(args: argparse.Namespace) -> None:
    """Create a human override for an evaluation."""
    config = _load_config(args)

    verdict_store = None
    if config.verdict is not None:
        from nthlayer_learn import SQLiteVerdictStore
        verdict_store = SQLiteVerdictStore(config.verdict.store_path)

    from nthlayer_measure.store.sqlite import SQLiteScoreStore
    store = SQLiteScoreStore(config.store.path, verdict_store=verdict_store)

    corrected_dimensions: dict[str, float] = {}
    for d in args.dimension:
        if "=" not in d:
            print(
                f"Error: dimension must be name=score (got '{d}')",
                file=sys.stderr,
            )
            sys.exit(1)
        name, val = d.split("=", 1)
        score = float(val)
        if not (0.0 <= score <= 1.0):
            print(
                f"Error: score must be between 0.0 and 1.0 (got {score} for '{name}')",
                file=sys.stderr,
            )
            sys.exit(1)
        corrected_dimensions[name] = score

    async def _run():
        await store.save_override(args.eval_id, corrected_dimensions, args.corrector)

    asyncio.run(_run())
    result = {
        "eval_id": args.eval_id,
        "corrector": args.corrector,
        "corrected_dimensions": corrected_dimensions,
    }
    print(json.dumps(result, indent=2))


def cmd_overrides_list(args: argparse.Namespace) -> None:
    """List recent overrides."""
    from datetime import datetime, timedelta, timezone

    config = _load_config(args)
    store = _build_store(config)

    async def _run():
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        return await store.get_overrides(
            since=since, limit=100, agent_name=args.agent
        )

    overrides = asyncio.run(_run())
    print(json.dumps(overrides, indent=2, default=str))


def cmd_governance_show(args: argparse.Namespace) -> None:
    """Show agent autonomy + governance log."""
    config = _load_config(args)
    store = _build_store(config)

    async def _run():
        autonomy = await store.get_autonomy(args.agent_name)
        return autonomy

    autonomy = asyncio.run(_run())
    result = {
        "agent_name": args.agent_name,
        "autonomy": autonomy or "full",
    }
    print(json.dumps(result, indent=2))


def cmd_governance_restore(args: argparse.Namespace) -> None:
    """Restore autonomy (requires --approver)."""
    config = _load_config(args)
    store = _build_store(config)
    tracker = _build_tracker(store)

    from nthlayer_measure.governance.engine import ErrorBudgetGovernance

    governance = ErrorBudgetGovernance(
        store=store,
        tracker=tracker,
        window_days=config.governance.error_budget_window_days,
        threshold=config.governance.error_budget_threshold,
    )

    level = AutonomyLevel(args.level)

    async def _run():
        await governance.restore_autonomy(args.agent_name, level, args.approver)

    asyncio.run(_run())
    print(json.dumps({
        "agent_name": args.agent_name,
        "restored_to": args.level,
        "approver": args.approver,
    }, indent=2))


# --- Main ---


def main() -> None:
    """Entry point with subcommands."""
    parser = argparse.ArgumentParser(
        prog="nthlayer-measure",
        description="nthlayer-measure — AI agent quality measurement",
    )
    parser.add_argument(
        "-c", "--config",
        type=Path,
        default=Path("measure.yaml"),
        help="Path to measure.yaml config file",
    )
    subparsers = parser.add_subparsers(dest="command")

    # serve
    subparsers.add_parser("serve", help="Start the evaluation pipeline")

    # evaluate
    eval_parser = subparsers.add_parser("evaluate", help="One-shot evaluation")
    eval_parser.add_argument("file", nargs="?", type=Path, default=None)
    eval_parser.add_argument("--agent-name", required=True)
    eval_parser.add_argument("--task-id", default="cli-eval")
    eval_parser.add_argument("--output-type", default="text")

    # status
    status_parser = subparsers.add_parser("status", help="Show agent status")
    status_parser.add_argument("agent_name")
    status_parser.add_argument("--window-days", type=int, default=7)

    # calibrate
    cal_parser = subparsers.add_parser("calibrate", help="Run calibration report")
    cal_parser.add_argument("--window-days", type=int, default=30)
    cal_parser.add_argument("--agent", type=str, default=None)
    cal_parser.add_argument(
        "--verdict", action="store_true", default=False,
        help="Use verdict-based calibration (system-wide)",
    )

    # overrides
    ov_parser = subparsers.add_parser("overrides", help="Override management")
    ov_sub = ov_parser.add_subparsers(dest="overrides_command")
    list_parser = ov_sub.add_parser("list", help="List recent overrides")
    list_parser.add_argument("--days", type=int, default=7)
    list_parser.add_argument("--agent", type=str, default=None)
    create_parser = ov_sub.add_parser("create", help="Create a human override")
    create_parser.add_argument("eval_id", help="Evaluation ID to override")
    create_parser.add_argument("--corrector", required=True, help="Who is overriding (e.g. human:rob)")
    create_parser.add_argument(
        "--dimension", action="append", required=True,
        help="Corrected dimension as name=score (repeatable)",
    )

    # governance
    gov_parser = subparsers.add_parser("governance", help="Governance management")
    gov_sub = gov_parser.add_subparsers(dest="gov_command")
    show_parser = gov_sub.add_parser("show", help="Show agent governance")
    show_parser.add_argument("agent_name")
    restore_parser = gov_sub.add_parser("restore", help="Restore autonomy")
    restore_parser.add_argument("agent_name")
    restore_parser.add_argument(
        "level",
        choices=[l.value for l in AutonomyLevel],
    )
    restore_parser.add_argument("--approver", required=True)

    args = parser.parse_args()

    handlers = {
        "serve": cmd_serve,
        "evaluate": cmd_evaluate,
        "status": cmd_status,
        "calibrate": cmd_calibrate,
        "governance": _dispatch_governance,
        "overrides": _dispatch_overrides,
        None: cmd_serve,  # default
    }

    handler = handlers.get(args.command, cmd_serve)
    handler(args)


def _dispatch_governance(args: argparse.Namespace) -> None:
    if args.gov_command == "show":
        cmd_governance_show(args)
    elif args.gov_command == "restore":
        cmd_governance_restore(args)
    else:
        print("Usage: nthlayer-measure governance {show,restore}", file=sys.stderr)
        sys.exit(1)


def _dispatch_overrides(args: argparse.Namespace) -> None:
    if args.overrides_command == "list":
        cmd_overrides_list(args)
    elif args.overrides_command == "create":
        cmd_overrides_create(args)
    else:
        print("Usage: nthlayer-measure overrides {list,create}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
