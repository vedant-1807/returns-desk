from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from returns_desk.auth import mint_token
from returns_desk.clock import SystemClock
from returns_desk.config import get_settings
from returns_desk.migrate import migrate
from returns_desk.processor.sandbox import SandboxProcessor
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

        processor = SandboxProcessor(settings.processor_dsn, SystemClock())
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
            processor.close()
        print(f"seeded fixture: {args.fixture} (embeddings: {settings.embedder})")

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
        print(json.dumps({k: v for k, v in metrics.items() if k != "by_kind"}, indent=2))
        print(f"report: {report}")
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


# Kept for `python -m returns_desk.cli`.
if __name__ == "__main__":
    main()
