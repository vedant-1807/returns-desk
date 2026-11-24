from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from returns_desk.auth import mint_token
from returns_desk.clock import SystemClock
from returns_desk.config import get_settings
from returns_desk.container import build_processor
from returns_desk.db import Principal
from returns_desk.migrate import migrate
from returns_desk.retrieval.embedders import build_embedder
from returns_desk.seeds import FIXTURES, Ids, seed
from returns_desk.seeds.demo import MERCHANTS, STAFF, USERS


def main() -> None:
    parser = argparse.ArgumentParser(prog="returns-desk")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("migrate", help="apply pending database migrations")

    seed_cmd = commands.add_parser("seed", help="reset the database and load a named fixture")
    seed_cmd.add_argument("--fixture", choices=FIXTURES, default="two_jackets")

    token_cmd = commands.add_parser("token", help="print a local access token for a seeded user")
    token_cmd.add_argument("user", choices=sorted(USERS))
    token_cmd.add_argument("merchant", choices=sorted(MERCHANTS))

    commands.add_parser("ids", help="print the IDs of the seeded reference order")
    commands.add_parser("reconcile", help="run due refund execution and reconciliation once")

    ops_cmd = commands.add_parser("ops", help="print the operations summary for one merchant")
    ops_cmd.add_argument("merchant", choices=sorted(MERCHANTS))
    ops_cmd.add_argument("--days", type=int, default=30)

    trace_cmd = commands.add_parser("trace", help="print the trace of one case as JSON")
    trace_cmd.add_argument("case_id")

    replay_cmd = commands.add_parser(
        "replay", help="run a recorded conversation again and compare the outcome"
    )
    replay_cmd.add_argument("bundle", help="path to a bundle JSON file")

    commands.add_parser("export-traces", help="send unsent trace spans to Langfuse once")

    users_cmd = commands.add_parser(
        "supabase-users", help="create the seeded demo people in Supabase Auth"
    )
    users_cmd.add_argument("--url", required=True, help="https://<project>.supabase.co")
    users_cmd.add_argument(
        "--service-role-key-env",
        default="SUPABASE_SERVICE_ROLE_KEY",
        help="environment variable that holds the project's service-role key",
    )
    users_cmd.add_argument(
        "--password-env",
        default="RD_DEMO_PASSWORD",
        help="environment variable that holds the shared demo password",
    )

    cron_cmd = commands.add_parser(
        "cron-sql", help="print the SQL that makes Supabase call reconcile every minute"
    )
    cron_cmd.add_argument("--api-url", required=True, help="the deployed API's base URL")

    eval_cmd = commands.add_parser("eval", help="run the synthetic eval suite and write a report")
    eval_cmd.add_argument("--suite", choices=("smoke", "nightly"), default="smoke")
    eval_cmd.add_argument("--cases", type=int, help="override the number of cases")
    eval_cmd.add_argument("--seed", type=int, default=7)
    eval_cmd.add_argument("--label", help="a name for this run, such as the model used")
    eval_cmd.add_argument("--out", help="directory for results.json and report.md")
    eval_cmd.add_argument(
        "--gate", action="store_true", help="exit with an error if the release gate fails"
    )
    eval_cmd.add_argument("--quiet", action="store_true", help="do not print each case")
    eval_cmd.add_argument(
        "--judge",
        action="store_true",
        help="have the calibrated LLM judge check policy answers against their citations",
    )
    eval_cmd.add_argument(
        "--compare-router",
        action="store_true",
        help="run the same cases routed and always-strong, and report the difference",
    )
    eval_cmd.add_argument(
        "--record",
        action="store_true",
        help="save the run's summary in the app database for the operations view",
    )
    eval_cmd.add_argument(
        "--min-accuracy",
        type=float,
        default=0.8,
        help="final-state accuracy the gate requires (0.8 suits the keyword baseline)",
    )

    para_cmd = commands.add_parser(
        "eval-paraphrase", help="have a model write rewrites of the eval templates"
    )
    para_cmd.add_argument("--variants", type=int, default=4, help="rewrites to ask for each")

    judge_cmd = commands.add_parser(
        "eval-judge", help="measure the LLM judge against the hand-labelled answers"
    )
    judge_cmd.add_argument("--out", default="evals/reports/judge-calibration")

    args = parser.parse_args()
    settings = get_settings()

    if args.command == "migrate":
        applied = migrate(settings)
        print("applied:", ", ".join(applied) if applied else "nothing new")

    elif args.command == "seed":
        import httpx2

        processor = build_processor(settings, SystemClock())
        try:
            seed(
                settings.admin_dsn,
                processor,
                fixture=args.fixture,
                now=datetime.now(UTC),
                embedder=build_embedder(settings),
            )
        except httpx2.HTTPError as exc:
            # Policy text is embedded while seeding, so the embedding server must be up.
            raise SystemExit(
                f"Could not embed the policy text with RD_EMBEDDER={settings.embedder} "
                f"({exc!r}).\nStart the local model server with scripts/local-models.sh, "
                "or set RD_EMBEDDER=hash to seed without a model."
            ) from None
        finally:
            processor.close()  # type: ignore[attr-defined]
        print(
            f"seeded fixture: {args.fixture} (embeddings: {settings.embedder}, "
            f"payments: {settings.payment_provider})"
        )

    elif args.command == "token":
        surface = "staff" if args.user in STAFF else "shopper"
        print(
            mint_token(
                settings,
                user_id=Ids.user(args.user),
                merchant_id=Ids.merchant(args.merchant),
                surface=surface,
                now=datetime.now(UTC),
            )
        )

    elif args.command == "ids":
        out = {
            slug: {
                "order_id": str(Ids.order(slug, "1001")),
                "blue_line_id": str(Ids.line(slug, "1001", "blue")),
                "green_line_id": str(Ids.line(slug, "1001", "green")),
            }
            for slug in MERCHANTS
        }
        print(json.dumps(out, indent=2))

    elif args.command == "reconcile":
        from returns_desk.container import build_container

        container = build_container(settings)
        try:
            print(json.dumps(container.execution.run_due()))
        finally:
            container.close()

    elif args.command == "ops":
        from returns_desk.container import build_container
        from returns_desk.services import ops

        container = build_container(settings)
        try:
            with container.db.tenant_tx(_staff_view(args.merchant)) as conn:
                result = ops.summary(
                    conn,
                    _staff_view(args.merchant),
                    now=container.clock.now(),
                    window_days=args.days,
                )
            print(json.dumps(result, indent=2, default=str))
        finally:
            container.close()

    elif args.command == "trace":
        import psycopg
        from psycopg.rows import dict_row

        from returns_desk.tracing.view import case_trace

        # An admin read of the whole trace, restricted fields included.
        with psycopg.connect(settings.admin_dsn, row_factory=dict_row) as conn:
            found = conn.execute(
                "select m.slug from cases c join merchants m on m.id = c.merchant_id "
                "where c.id = %s",
                (args.case_id,),
            ).fetchone()
            if found is None:
                raise SystemExit(f"no case {args.case_id}")
            trace = case_trace(conn, _staff_view(found["slug"]), UUID(args.case_id))
        print(json.dumps(trace, indent=2, default=str))

    elif args.command == "replay":
        from pathlib import Path

        from returns_desk.tracing import bundle as bundles

        result = bundles.replay(settings, json.loads(Path(args.bundle).read_text()))
        print(json.dumps(result, indent=2, default=str))
        if not result["matches"]:
            raise SystemExit(1)

    elif args.command == "export-traces":
        from returns_desk.container import build_container

        container = build_container(settings)
        try:
            if not container.tracer.enabled:
                raise SystemExit(
                    "Langfuse is not configured. Set RD_LANGFUSE_PUBLIC_KEY and "
                    "RD_LANGFUSE_SECRET_KEY."
                )
            print(json.dumps(container.tracer.export_new()))
        finally:
            container.close()

    elif args.command == "supabase-users":
        import os

        from returns_desk.deploy import create_supabase_users

        key = os.environ.get(args.service_role_key_env, "")
        password = os.environ.get(args.password_env, "")
        if not key or not password:
            raise SystemExit(
                f"Set {args.service_role_key_env} and {args.password_env} in the environment. "
                "Neither is ever passed on the command line."
            )
        for row in create_supabase_users(args.url, key, password):
            print(f"{row['result']:8} {row['email']}")

    elif args.command == "cron-sql":
        from returns_desk.deploy import reconcile_cron_sql

        print(reconcile_cron_sql(args.api_url, settings.internal_token))

    elif args.command == "eval-paraphrase":
        from returns_desk.evals import paraphrase
        from returns_desk.evals.cases import TEMPLATES
        from returns_desk.models.gateway import build_gateway

        _need_a_model(settings, "write rewrites of the eval templates")
        gateway = build_gateway(_patient(settings))
        built = paraphrase.build(
            gateway, TEMPLATES, variants=args.variants, label=_model_label(settings)
        )
        paraphrase.PARAPHRASE_FILE.write_text(json.dumps(built, indent=1) + "\n")
        print(
            f"kept {built['accepted_count']} rewrites, dropped {built['dropped_count']} that "
            f"changed the facts, {built['failed_calls']} calls failed"
        )
        print(f"saved: {paraphrase.PARAPHRASE_FILE}")

    elif args.command == "eval-judge":
        from pathlib import Path

        from returns_desk.evals import judge as judging
        from returns_desk.models.gateway import build_gateway

        _need_a_model(settings, "calibrate the judge")
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        judge = judging.Judge(
            build_gateway(_patient(settings)),
            _model_label(settings),
            Path("artifacts/evals/judge-cache.json"),
        )
        result = {**judging.calibrate(judge), "judge_model": _model_label(settings)}
        (out / "results.json").write_text(json.dumps(result, indent=2) + "\n")
        (out / "report.md").write_text(_judge_report(result))
        print(json.dumps(result, indent=2))
        print(f"report: {out / 'report.md'}")

    elif args.command == "eval":
        import logging
        from pathlib import Path

        from returns_desk.evals.cases import generate
        from returns_desk.evals.report import check_gate, write_comparison, write_report
        from returns_desk.evals.runner import judge_answers, run_cases, score

        for noisy in ("httpx2", "httpx", "mcp"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        count = args.cases or {"smoke": 30, "nightly": 300}[args.suite]
        label = args.label or f"{settings.model_routes} models, {settings.embedder} embeddings"
        cases = generate(count, seed=args.seed)
        out = Path(args.out or f"artifacts/evals/{args.suite}")

        judge = None
        calibration = None
        if args.judge:
            from returns_desk.evals import judge as judging
            from returns_desk.models.gateway import build_gateway

            _need_a_model(settings, "judge policy answers")
            calibration_file = Path("evals/reports/judge-calibration/results.json")
            if not calibration_file.exists():
                raise SystemExit(
                    "The judge has not been calibrated. Run `returns-desk eval-judge` first."
                )
            calibration = json.loads(calibration_file.read_text())
            judge = judging.Judge(
                build_gateway(_patient(settings)),
                _model_label(settings),
                Path("artifacts/evals/judge-cache.json"),
            )

        def run(run_settings: Any, run_label: str, run_out: Path) -> dict[str, Any]:
            print(f"running {count} cases ({args.suite}, seed {args.seed}) with {run_label}")
            results = run_cases(run_settings, cases, progress=not args.quiet)
            if judge is not None:
                judge_answers(results, judge)
            run_metrics = score(results, calibration)
            path = write_report(
                run_out,
                suite=args.suite,
                seed=args.seed,
                label=run_label,
                metrics=run_metrics,
                results=results,
            )
            print(f"report: {path}")
            return run_metrics

        if args.compare_router:
            # The same cases twice: once routed, once with the stronger model for
            # everything. The difference in accuracy is what routing costs in quality.
            _need_a_model(settings, "compare routers")
            both = {
                router: run(
                    settings.model_copy(update={"model_router": router}),
                    f"{label}, {router.replace('_', ' ')}",
                    out / router,
                )
                for router in ("routed", "always_strong")
            }
            path = write_comparison(out, suite=args.suite, seed=args.seed, label=label, runs=both)
            print(f"comparison: {path}")
            metrics = both["routed"]
        else:
            metrics = run(settings, label, out)

        shown = {k: v for k, v in metrics.items() if k not in ("by_kind", "by_merchant")}
        print(json.dumps(shown, indent=2))
        if args.record:
            import psycopg
            from psycopg.types.json import Jsonb

            with psycopg.connect(settings.admin_dsn) as conn:
                conn.execute(
                    """
                    insert into eval_runs (suite, label, seed, cases, metrics, by_merchant)
                    values (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        args.suite,
                        label,
                        args.seed,
                        metrics["cases"],
                        Jsonb({k: v for k, v in metrics.items() if k != "by_merchant"}),
                        Jsonb(metrics["by_merchant"]),
                    ),
                )
            print("recorded for the operations view")
        failures = check_gate(metrics, args.min_accuracy)
        if failures:
            print("release gate: FAILED")
            for failure in failures:
                print(f"  - {failure}")
            if args.gate:
                raise SystemExit(1)
        else:
            print("release gate: passed")

    else:  # pragma: no cover
        raise SystemExit(f"unknown command {args.command}")


def _need_a_model(settings: Any, to_do: str) -> None:
    if "keyword" in settings.model_routes.split(","):
        raise SystemExit(
            f"A real model is needed to {to_do}. Set RD_MODEL_ROUTES to a model route "
            "(for example groq or ollama)."
        )


def _patient(settings: Any) -> Any:
    """Settings for a batch job: wait out a rate limit instead of failing over, and give
    each call room to finish."""
    return settings.model_copy(
        update={
            "model_rate_limit_wait_seconds": max(settings.model_rate_limit_wait_seconds, 60.0),
            "model_deadline_seconds": max(settings.model_deadline_seconds, 60.0),
        }
    )


def _model_label(settings: Any) -> str:
    """Which models stand behind the configured routes, strongest tier named last."""
    names = []
    for route in settings.model_routes.split(","):
        route = route.strip()
        strong = getattr(settings, f"{route}_model_strong", None)
        names.append(f"{route}/{strong}" if strong else route)
    return ", ".join(names)


def _judge_report(result: dict[str, Any]) -> str:
    rows = "\n".join(
        f"| {d['id']} | {'faithful' if d['label'] else 'not faithful'} | "
        f"{'faithful' if d['judge'] else 'not faithful'} |"
        for d in result["disagreements"]
    )
    return f"""# Judge calibration: citation faithfulness

The judge is a model that reads a policy answer and the passages it cites, and says
whether every factual claim in the answer is stated in those passages. Before its
verdicts are used, it is run over answers that were labelled by hand.

| | |
| --- | --- |
| Judge model | {result["judge_model"]} |
| Prompt version | {result["prompt_version"]} |
| Hand-labelled answers | {result["examples"]} ({result["label_faithful"]} faithful) |
| Answers the judge gave a verdict on | {result["judged"]} |
| Agreement with the labels | {result["agreement"]:.1%} |
| **Cohen's kappa** | **{result["cohens_kappa"]}** |

Kappa is agreement corrected for chance: 1 is perfect, 0 is no better than guessing.

## Where the judge and the labels disagree ({len(result["disagreements"])})

| Example | Hand label | Judge |
| --- | --- | --- |
{rows or "| none | | |"}

## What the labels are

The examples are in `src/returns_desk/evals/data/judge_calibration.json`. They were
written and labelled by the builder of this project (an AI assistant) and have not been
reviewed by a person yet. Until someone does, treat the kappa as agreement with those
labels, not with a human rater.
"""


def _staff_view(merchant: str) -> Principal:
    """A read-only supervisor's view of one seeded merchant, for admin commands. It names
    no real user: row-level security only needs the merchant and the role."""
    return Principal(
        user_id=UUID(int=0),
        merchant_id=Ids.merchant(merchant),
        role="supervisor",
        shopper_id=None,
        merchant_timezone=MERCHANTS[merchant]["timezone"],
        merchant_slug=merchant,
    )


# Kept for `python -m returns_desk.cli`.
if __name__ == "__main__":
    main()
