#!/usr/bin/env python3
"""Router-only eval: does the delegation router pick the right mode, and fast?

Runs ONLY the router (``agent.delegation_router.route``) over the cases in
``tests/eval/delegation_modes.jsonl`` x N reps.  No chat agent, no tools, no
helpers: one auxiliary-model call per run, scored against the case's
``expected_mode``.  It prints per-case modes, accuracy and p50 / p95 latency.

The router is exercised the way the turn prologue exercises it, minus the
agent gating: explicit overrides ("do it yourself" / "use helpers") are decided
in code and never reach the model; they are scored but excluded from latency.

Uses the auxiliary client of the chosen profile (``auxiliary.delegation_router``
in its config.yaml), so the numbers are for the model that profile would use.
``CLOVER_HOME`` is pointed at the profile before anything is imported.

Exit status: 0 ok (or accuracy >= --min-accuracy), 1 accuracy below the bar,
2 usage / harness error.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = REPO_ROOT / "tests" / "eval" / "delegation_modes.jsonl"
MODES = ("direct", "single", "parallel", "staged")
# What the case prompts' {workdir} placeholder becomes: the router never touches
# the path, it only has to look like one.
WORKDIR_STUB = "/work/eval"


def load_cases(path: Path) -> List[Dict[str, Any]]:
    cases: List[Dict[str, Any]] = []
    seen = set()
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        for key in ("id", "prompt", "expected_mode"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ValueError(f"{path}:{lineno}: missing or empty {key!r}")
        if case["expected_mode"] not in MODES:
            raise ValueError(f"{path}:{lineno}: expected_mode {case['expected_mode']!r} not in {MODES}")
        if case["id"] in seen:
            raise ValueError(f"{path}:{lineno}: duplicate case id {case['id']!r}")
        seen.add(case["id"])
        cases.append(case)
    if not cases:
        raise ValueError(f"{path}: no cases")
    return cases


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Nearest-rank percentile (``q`` in 0..100); ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def run_router_eval(
    cases: Sequence[Dict[str, Any]],
    route_fn: Callable[..., Any],
    *,
    reps: int = 1,
    clock: Callable[[], float] = time.perf_counter,
    progress: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    """One record per case x rep.  ``route_fn(message, recent_turns=())`` is the
    router; an exception is recorded, never raised."""
    say = progress or (lambda _m: None)
    runs: List[Dict[str, Any]] = []
    for case in cases:
        prompt = case["prompt"].replace("{workdir}", WORKDIR_STUB)
        for rep in range(max(1, reps)):
            run: Dict[str, Any] = {
                "case_id": case["id"], "rep": rep, "expected_mode": case["expected_mode"],
                "mode": None, "source": None, "reason": "", "latency_ms": 0,
                "correct": False, "error": None,
            }
            t0 = clock()
            try:
                verdict = route_fn(prompt, [])
                run["latency_ms"] = int(round((clock() - t0) * 1000))
                if verdict is not None:
                    run["mode"] = verdict.mode
                    run["source"] = getattr(verdict, "source", None)
                    run["reason"] = getattr(verdict, "reason", "") or ""
            except Exception as exc:  # one bad call must not lose the others
                run["latency_ms"] = int(round((clock() - t0) * 1000))
                run["error"] = f"{type(exc).__name__}: {exc}"
            run["correct"] = run["mode"] == case["expected_mode"]
            runs.append(run)
            say(f"{case['id']} r{rep}: expected {case['expected_mode']}, got {run['mode']} "
                f"({run['latency_ms']} ms)" + (f"  [{run['error']}]" if run["error"] else ""))
    return runs


def summarize(runs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    cases: Dict[str, Dict[str, Any]] = {}
    for r in runs:
        c = cases.setdefault(r["case_id"], {
            "expected_mode": r["expected_mode"], "modes": [], "correct": 0, "n": 0,
        })
        c["modes"].append(r["mode"])
        c["n"] += 1
        c["correct"] += 1 if r["correct"] else 0
    n = len(runs)
    correct = sum(1 for r in runs if r["correct"])
    # Overrides never reach the model, so they say nothing about its latency.
    timed = [r["latency_ms"] for r in runs if r.get("source") != "override"]
    return {
        "n": n,
        "correct": correct,
        "accuracy": (correct / n) if n else 0.0,
        "failed_routes": sum(1 for r in runs if r["mode"] is None),
        "errors": sum(1 for r in runs if r.get("error")),
        "p50_ms": percentile(timed, 50),
        "p95_ms": percentile(timed, 95),
        "cases": cases,
    }


def render(summary: Dict[str, Any]) -> str:
    lines = [
        "Delegation router eval",
        f"{'case':<26}{'expect':<10}{'got (per rep)':<44}{'ok':<8}",
    ]
    for case_id, c in summary["cases"].items():
        got = ",".join(m or "NONE" for m in c["modes"])
        lines.append(
            f"{case_id[:25]:<26}{c['expected_mode']:<10}{got[:43]:<44}{c['correct']}/{c['n']:<6}"
        )
    p50, p95 = summary["p50_ms"], summary["p95_ms"]
    lines += [
        "",
        f"accuracy {summary['correct']}/{summary['n']} = {summary['accuracy'] * 100:.1f}%"
        f"   failed routes {summary['failed_routes']}   errors {summary['errors']}",
        f"latency  p50 {'-' if p50 is None else str(p50) + ' ms'}   "
        f"p95 {'-' if p95 is None else str(p95) + ' ms'}   (model calls only)",
    ]
    return "\n".join(lines) + "\n"


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Router-only delegation eval (no chat agent).")
    p.add_argument("--profile", default=None,
                   help="Clover profile whose auxiliary.delegation_router config to use "
                        "(default: the current CLOVER_HOME)")
    p.add_argument("--cases", default=str(DEFAULT_CASES), help="Cases JSONL")
    p.add_argument("--reps", type=int, default=3, help="Repetitions per case (default 3)")
    p.add_argument("--only", action="append", default=[], help="Run only these case ids (comma separated)")
    p.add_argument("--out", required=True, help="Output directory (results.json, summary.txt)")
    p.add_argument("--no-warmup", action="store_true",
                   help="Time the first model call too (it pays the one-time SDK imports and "
                        "client set-up, which a long-lived gateway pays once at start-up)")
    p.add_argument("--min-accuracy", type=float, default=0.0,
                   help="Exit 1 when accuracy is below this fraction (default 0 = report only)")
    return p.parse_args(argv)


def _resolve_profile_home(profile: str) -> Path:
    from clover_cli.profiles import get_profile_dir, profile_exists

    if not profile_exists(profile):
        raise SystemExit(f"profile {profile!r} does not exist")
    return Path(get_profile_dir(profile))


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    route_fn: Optional[Callable[..., Any]] = None,
) -> int:
    args = _parse_args(argv)
    try:
        cases = load_cases(Path(args.cases))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.only:
        wanted = {i.strip() for chunk in args.only for i in chunk.split(",") if i.strip()}
        unknown = wanted - {c["id"] for c in cases}
        if unknown:
            print(f"error: unknown case id(s): {sorted(unknown)}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c["id"] in wanted]

    if route_fn is None:
        if args.profile:
            os.environ["CLOVER_HOME"] = str(_resolve_profile_home(args.profile))
        sys.path.insert(0, str(REPO_ROOT))
        from agent.delegation_router import load_settings, route

        settings = load_settings()

        def route_fn(message: str, recent_turns: Sequence[Any] = ()) -> Any:  # noqa: F811
            return route(message, recent_turns, settings=settings)

        if not args.no_warmup:
            # One untimed call so the measured runs see the steady state a
            # long-lived gateway process sees, not the process's first call.
            from dataclasses import replace

            t0 = time.perf_counter()
            route("Which city is the capital of France?", [],
                  settings=replace(settings, timeout_seconds=max(settings.timeout_seconds, 180.0)))
            print(f"warm-up call took {time.perf_counter() - t0:.1f}s (not measured)",
                  file=sys.stderr, flush=True)

    runs = run_router_eval(
        cases, route_fn, reps=args.reps,
        progress=lambda m: print(m, file=sys.stderr, flush=True),
    )
    summary = summarize(runs)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps({"summary": summary, "runs": runs, "profile": args.profile,
                    "reps": args.reps, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
                   indent=2, default=str),
        encoding="utf-8",
    )
    text = render(summary)
    (out_dir / "summary.txt").write_text(text, encoding="utf-8")
    print(text)
    return 0 if summary["accuracy"] >= args.min_accuracy else 1


if __name__ == "__main__":
    sys.exit(main())
