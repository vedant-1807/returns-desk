from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
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

    elif args.command == "eval":
        import logging
        from pathlib import Path

        from returns_desk.evals.cases import generate
        from returns_desk.evals.report import check_gate, write_report
        from returns_desk.evals.runner import run_cases, score

        for noisy in ("httpx2", "httpx", "mcp"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        count = args.cases or {"smoke": 30, "nightly": 300}[args.suite]
        label = args.label or f"{settings.model_routes} models, {settings.embedder} embeddings"
        cases = generate(count, seed=args.seed)
        print(f"running {count} cases ({args.suite}, seed {args.seed}) with {label}")
        results = run_cases(settings, cases, progress=not args.quiet)
        metrics = score(results)
        out = Path(args.out or f"artifacts/evals/{args.suite}")
        report = write_report(
            out, suite=args.suite, seed=args.seed, label=label, metrics=metrics, results=results
        )
        shown = {k: v for k, v in metrics.items() if k not in ("by_kind", "by_merchant")}
        print(json.dumps(shown, indent=2))
        print(f"report: {report}")
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
