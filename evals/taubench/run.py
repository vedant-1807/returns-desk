"""Run a pinned subset of the τ-bench retail benchmark and report pass^k.

    uv run python evals/taubench/run.py --tasks 20 --trials 3

τ-bench (Sierra) scores a tool-calling agent on simulated retail support conversations: an
LLM plays the customer, the agent calls the benchmark's own tools, and a task passes only
if the final database equals the expected one. pass^k is the chance that a task passes in
all of k independent tries.

Everything that affects the score is pinned and written into the report:

    benchmark   sierra-research/tau-bench at PINNED_COMMIT, retail domain, test split
    agent       the benchmark's own ToolCallingAgent, unmodified
    tools       the benchmark's retail tools, unmodified
    user        the benchmark's LLM-simulated user, unmodified
    models      set on the command line; the defaults are local, so a run costs nothing
    subset      the first N tasks of the test split, by index

The benchmark imports `litellm`. This runner puts a small stand-in for it first on the
import path (evals/taubench/shim/litellm.py) so no LiteLLM install is needed.

This measures the model behind the gateway on a standard retail-agent benchmark. It does
not run the Returns Desk workflow, where the model is given no tools at all.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from math import comb
from pathlib import Path
from typing import Any

REPO = "https://github.com/sierra-research/tau-bench"
PINNED_COMMIT = "59a200c6d575d595120f1cb70fea53cef0632f6b"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEFAULT_SOURCE = ROOT / ".cache" / "tau-bench-src"


def fetch(source: Path) -> None:
    """Clone the benchmark at the pinned commit if it is not there, then check the pin."""
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", REPO, str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "checkout", "--quiet", PINNED_COMMIT], check=True)
    head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if head != PINNED_COMMIT:
        raise SystemExit(
            f"{source} is at {head}, not the pinned {PINNED_COMMIT}. "
            "Check out the pinned commit or delete the folder to fetch it again."
        )


def pass_hat_k(passes_per_task: dict[int, int], trials: int) -> dict[int, float]:
    """pass^k as the benchmark computes it: for each task, the share of k-subsets of its
    trials that all passed; then the mean over tasks."""
    out = {}
    for k in range(1, trials + 1):
        total = sum(comb(c, k) / comb(trials, k) for c in passes_per_task.values())
        out[k] = round(total / len(passes_per_task), 4)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--tasks", type=int, default=20, help="first N tasks of the test split")
    parser.add_argument("--trials", type=int, default=3, help="independent tries per task")
    parser.add_argument("--model", default="llama3.1:8b", help="the agent's model")
    parser.add_argument("--base-url", default="http://localhost:11436/v1")
    parser.add_argument("--user-model", default=None, help="defaults to the agent's model")
    parser.add_argument("--user-base-url", default=None, help="defaults to --base-url")
    parser.add_argument(
        "--api-key-env", default="", help="name of an environment variable holding an API key"
    )
    parser.add_argument("--temperature", type=float, default=0.0, help="the agent's temperature")
    parser.add_argument(
        "--user-temperature",
        type=float,
        default=None,
        help="the simulated user's temperature (default: the server's own default)",
    )
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--label", default=None)
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "taubench"))
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    args = parser.parse_args()

    source = Path(args.source)
    fetch(source)
    # The stand-in for litellm must win over any installed copy; then the benchmark itself.
    sys.path.insert(0, str(source))
    sys.path.insert(0, str(HERE / "shim"))
    import litellm  # noqa: I001 - the stand-in in evals/taubench/shim
    from tau_bench.agents.tool_calling_agent import ToolCallingAgent
    from tau_bench.envs.retail import MockRetailDomainEnv

    api_key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    user_model = args.user_model or args.model
    litellm.configure("agent", base_url=args.base_url, api_key=api_key)
    litellm.configure(
        "user",
        base_url=args.user_base_url or args.base_url,
        api_key=api_key,
        user_temperature=args.user_temperature,
    )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "results.json"
    settings = {
        "benchmark": REPO,
        "commit": PINNED_COMMIT,
        "domain": "retail",
        "split": "test",
        "tasks": args.tasks,
        "trials": args.trials,
        "agent": "tau_bench.agents.tool_calling_agent.ToolCallingAgent",
        "agent_model": args.model,
        "agent_temperature": args.temperature,
        "user": "tau_bench.envs.user.LLMUserSimulationEnv",
        "user_model": user_model,
        "user_temperature": args.user_temperature,
        "max_steps": args.max_steps,
        "base_url": args.base_url,
        "user_base_url": args.user_base_url or args.base_url,
    }
    runs: list[dict[str, Any]] = []
    if results_path.exists():
        saved = json.loads(results_path.read_text())
        if saved.get("settings") == settings:
            runs = saved["runs"]
            print(f"resuming: {len(runs)} runs already done")
    done = {(run["task"], run["trial"]) for run in runs}

    total = args.tasks * args.trials
    started_all = time.monotonic()
    for trial in range(args.trials):
        for task in range(args.tasks):
            if (task, trial) in done:
                continue
            began = time.monotonic()
            record: dict[str, Any] = {"task": task, "trial": trial, "reward": 0.0, "error": None}
            try:
                env = MockRetailDomainEnv(
                    user_strategy="llm",
                    user_model=user_model,
                    user_provider="user",
                    task_split="test",
                    task_index=task,
                )
                agent = ToolCallingAgent(
                    tools_info=env.tools_info,
                    wiki=env.wiki,
                    model=args.model,
                    provider="agent",
                    temperature=args.temperature,
                )
                result = agent.solve(env=env, task_index=task, max_num_steps=args.max_steps)
                record["reward"] = float(result.reward)
                record["messages"] = len(result.messages)
                record["tool_calls"] = sum(1 for m in result.messages if m.get("role") == "tool")
            except Exception as exc:  # noqa: BLE001 - a broken run scores zero, as upstream does
                record["error"] = repr(exc)[:300]
            record["seconds"] = round(time.monotonic() - began, 1)
            runs.append(record)
            results_path.write_text(json.dumps({"settings": settings, "runs": runs}, indent=1))
            mark = "pass" if record["reward"] >= 1 - 1e-6 else "fail"
            print(
                f"  {len(runs):>4}/{total} trial {trial + 1} task {task:>3} {mark} "
                f"{record['seconds']}s" + (f" error: {record['error']}" if record["error"] else ""),
                flush=True,
            )

    passes: dict[int, int] = {task: 0 for task in range(args.tasks)}
    for run in runs:
        passes[run["task"]] += int(run["reward"] >= 1 - 1e-6)
    metrics = {
        "pass_hat_k": pass_hat_k(passes, args.trials),
        "average_reward": round(sum(run["reward"] for run in runs) / len(runs), 4),
        "runs": len(runs),
        "errors": sum(1 for run in runs if run["error"]),
        "tasks_passed_at_least_once": sum(1 for c in passes.values() if c > 0),
        "seconds_this_session": round(time.monotonic() - started_all, 1),
        "seconds_all_runs": round(sum(run["seconds"] for run in runs), 1),
        "model_calls_this_session": litellm.stats,
    }
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    label = args.label or f"{args.model} (agent and simulated user)"
    payload = {
        "settings": settings,
        "label": label,
        "generated_at": stamp,
        "metrics": metrics,
        "runs": runs,
    }
    results_path.write_text(json.dumps(payload, indent=1))

    k_rows = "\n".join(f"| pass^{k} | {value:.1%} |" for k, value in metrics["pass_hat_k"].items())
    per_task = "\n".join(
        f"| {task} | {passes[task]} of {args.trials} |" for task in range(args.tasks)
    )
    user_temperature = (
        "the server's default" if args.user_temperature is None else args.user_temperature
    )
    (out / "report.md").write_text(
        f"""# τ-bench retail subset ({label})

{args.tasks} tasks, {args.trials} tries each ({len(runs)} runs), generated {stamp}.

pass^k is the chance that a task passes in all of k independent tries. A task passes only
when the final database equals the expected one.

| Metric | Value |
| --- | --- |
{k_rows}
| Average reward | {metrics["average_reward"]:.1%} |
| Tasks passed at least once | {metrics["tasks_passed_at_least_once"]} of {args.tasks} |
| Runs that ended in an error | {metrics["errors"]} |

## What was pinned

| | |
| --- | --- |
| Benchmark | {REPO} at `{PINNED_COMMIT}` |
| Domain and split | retail, test split |
| Subset | the first {args.tasks} tasks by index (0 to {args.tasks - 1}) |
| Tries per task (k up to) | {args.trials} |
| Agent | the benchmark's own `ToolCallingAgent`, unmodified, at most {args.max_steps} steps |
| Tools | the benchmark's retail tools, unmodified |
| Agent model | `{args.model}`, temperature {args.temperature} |
| Simulated user | the benchmark's `LLMUserSimulationEnv`, unmodified |
| User model | `{user_model}`, temperature {user_temperature} |

## How to read this

- This measures the model on a standard retail-agent benchmark, using the benchmark's own
  agent loop. It does not run the Returns Desk workflow, where the model has no tools.
- The published τ-bench numbers use a much stronger model as the simulated user. Here the
  same small model plays both sides, so these numbers are not comparable with the
  leaderboard. A weak simulated user can fail a task the agent would have passed.
- The subset is small. Treat the result as a smoke reading, not a ranking.

## Passes per task

| Task | Passed |
| --- | --- |
{per_task}
"""
    )
    print(json.dumps(metrics, indent=2))
    print(f"report: {out / 'report.md'}")


if __name__ == "__main__":
    main()
