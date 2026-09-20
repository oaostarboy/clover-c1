#!/usr/bin/env python3
"""Run Clover's adversarial multi-model council end to end."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ROSTER_PATH = SKILL_DIR / "references" / "models.json"

MODE_SEATS = {
    "quick": ["STEELMAN", "PROSECUTOR", "PRAGMATIST"],
    "full": ["STEELMAN", "PROSECUTOR", "PREMISE", "PRAGMATIST", "OUTSIDER"],
    "deep": [
        "STEELMAN", "PROSECUTOR", "PREMISE", "PRAGMATIST", "OUTSIDER", "HISTORIAN"
    ],
}

LENSES = {
    "STEELMAN": (
        "Make the strongest HONEST case FOR the proposal and name the upside "
        "everyone is undervaluing. No cheerleading you cannot defend."
    ),
    "PROSECUTOR": (
        "Attack the proposal. Name the fatal flaw, hidden cost, and concrete way "
        "it blows up. Default to KILL IT unless it survives real scrutiny."
    ),
    "PREMISE": (
        "Challenge the question itself. What is actually being solved? Is that "
        "the real problem or a proxy? Is there a cheaper route to the true goal?"
    ),
    "PRAGMATIST": (
        "Find the smallest version that works. Weigh build effort, ongoing upkeep, "
        "and second-order effects. Reject over-engineering."
    ),
    "OUTSIDER": (
        "Assume no context that is not written. Name what is confusing, unjustified, "
        "or obvious to nobody outside the room."
    ),
    "HISTORIAN": "Search the active Clover memory for prior attempts and cite paths.",
}

NO_CHAT = (
    "Do not send messages or contact anyone. You are one private council seat. "
    "Write only the requested output file and its .done marker."
)


def clover_home() -> Path:
    return Path(os.environ.get("CLOVER_HOME", str(Path.home() / ".clover"))).expanduser()


def write_progress(
    work: Path,
    *,
    status: str,
    mode: str,
    stage: str,
    seat_done: int = 0,
    seat_total: int = 0,
    review_done: int = 0,
    review_total: int = 0,
    stalled: list[str] | None = None,
    started_at: float,
    now: float | None = None,
) -> None:
    """Atomically publish the small state consumed by gateway progress cards."""
    now = time.time() if now is None else now
    payload = {
        "status": status,
        "mode": mode,
        "stage": stage,
        "seat_done": seat_done,
        "seat_total": seat_total,
        "review_done": review_done,
        "review_total": review_total,
        "stalled": stalled or [],
        "elapsed_s": max(0, int(now - started_at)),
    }
    work.mkdir(parents=True, exist_ok=True)
    target = work / "progress.json"
    temporary = work / "progress.json.tmp"
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(target)


def load_roster(path: Path = DEFAULT_ROSTER_PATH) -> dict[str, dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("council model roster must be an object")
    required = set(MODE_SEATS["deep"]) | {"CHAIRMAN", "ATTACK"}
    missing = sorted(required - set(data))
    if missing:
        raise ValueError(f"council model roster is missing: {', '.join(missing)}")
    for seat in sorted(required):
        entry = data[seat]
        if not isinstance(entry, dict) or not entry.get("provider") or not entry.get("model"):
            raise ValueError(f"council seat {seat} needs provider and model")
    return data


def build_seat_command(
    seat: str,
    task: str,
    roster: dict[str, dict[str, str]],
    *,
    clover_bin: str,
    usage_file: Path | None = None,
) -> list[str]:
    try:
        route = roster[seat]
    except KeyError as exc:
        raise KeyError(f"No configured council route for {seat}") from exc
    command = [
        clover_bin,
        "-z",
        task,
        "-m",
        route["model"],
        "--provider",
        route["provider"],
    ]
    if usage_file is not None:
        command += ["--usage-file", str(usage_file)]
    return command


_PROVIDER_ALIASES = {("gemini-oauth", "custom")}


def validate_usage_route(
    seat: str,
    roster: dict[str, dict[str, str]],
    usage_file: Path,
) -> None:
    """Reject missing, failed, or substituted provider/model routes."""
    expected = roster[seat]
    try:
        usage = json.loads(usage_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{seat} produced no parseable route metadata") from exc
    actual_provider = str(usage.get("provider") or "")
    actual_model = str(usage.get("model") or "")
    provider_ok = (
        actual_provider == expected["provider"]
        or (expected["provider"], actual_provider) in _PROVIDER_ALIASES
    )
    if (
        usage.get("completed") is not True
        or not provider_ok
        or actual_model != expected["model"]
    ):
        raise RuntimeError(
            f"{seat} requested {expected['provider']}/{expected['model']} but "
            f"answered as {actual_provider or '?'}/{actual_model or '?'}"
        )


def parse_verdict(text: str) -> tuple[str, str, str]:
    def grab(key: str) -> str:
        match = re.search(rf"^\**\s*{key}\s*:\**\s*(.+)$", text, re.I | re.M)
        return match.group(1).strip().strip("*").strip() if match else ""

    return grab("VERDICT"), grab("NEXT"), grab("DISSENT")


def parse_severity(text: str) -> str:
    match = re.search(r"^\**\s*SEVERITY\s*:\**\s*([A-Za-z]+)", text, re.I | re.M)
    if not match:
        return "SERIOUS"
    value = match.group(1).upper()
    return value if value in {"FATAL", "SERIOUS", "MINOR", "SURVIVES"} else "SERIOUS"


def gist_of(text: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^\**\s*GIST\s*:\**\s*(.+)$", line.strip(), re.I)
        if match:
            return match.group(1).strip().strip("*")[:150]
    for line in text.splitlines():
        if line.strip():
            return line.strip().lstrip("#").strip()[:150]
    return "(empty answer)"


def scrub(text: str) -> str:
    result = text
    for name in list(LENSES) + ["PREMISE-CHECK", "STEEL-MAN"]:
        result = re.sub(rf"\b{re.escape(name)}\b", "[SEAT]", result, flags=re.I)
    return result


def seat_task(
    seat: str,
    question: str,
    output: Path,
    *,
    clover_home: Path | None = None,
) -> str:
    home = clover_home or globals()["clover_home"]()
    lens = LENSES[seat]
    if seat == "HISTORIAN":
        lens = (
            f"Search {home / 'MEMORY.md'}, {home / 'memories'}, and "
            f"{home / 'workspace'} for prior attempts or adjacent decisions. Report what "
            "was tried, what happened, and what it predicts. Cite file paths. If nothing "
            "exists, say so."
        )
    return (
        f"COUNCIL SEAT — {seat}. Answer blind; do not seek other seats' work.\n\n"
        f"{lens}\n\nTHE DECISION:\n{question}\n\n"
        f"Write to {output}. Line 1 must be `GIST: <position under 120 characters>`, "
        "then a blank line and a specific committed argument under 400 words. Do not "
        f"identify your seat in the answer. Create {output}.done only after the file is final.\n\n"
        f"{NO_CHAT}"
    )


def review_task(seat: str, question: str, anonymous: Path, output: Path, letters: list[str]) -> str:
    return (
        f"COUNCIL CROSS-REVIEW — {seat}.\n\nDecision:\n{question}\n\n"
        f"Read {anonymous}. It contains anonymous Responses {', '.join(letters)}. One is "
        "your own; you do not know which. Judge content only and refer to letters only.\n\n"
        f"Write to {output}, under 200 words: GIST line; ranking best-first with one reason "
        "each; largest blind spot; what all responses missed. Then create "
        f"{output}.done.\n\n{NO_CHAT}"
    )


def chairman_task(
    question: str,
    brief: Path,
    output: Path,
    mode: str,
    stalled: list[str],
) -> str:
    missing = f"Missing seats: {', '.join(stalled)}. Disclose that limitation.\n" if stalled else ""
    return (
        f"COUNCIL CHAIRMAN ({mode}). Commit to one result; hedging has failed.\n\n"
        f"DECISION:\n{question}\n{missing}\nRead {brief}.\n\n"
        f"Write to {output}. The first three lines must be exactly:\n"
        "VERDICT: <committed decision>\nNEXT: <one concrete action>\n"
        "DISSENT: <strongest point this verdict rejects>\n\n"
        "Then under 400 words cover agreement, clashes, review-found blind spots, and "
        "which answer ranked strongest. If one missing fact decides it, make obtaining "
        f"that fact the NEXT action. Create {output}.done when final.\n\n{NO_CHAT}"
    )


def attack_task(question: str, brief: Path, verdict: str, next_step: str, output: Path) -> str:
    return (
        "COUNCIL POST-VERDICT ATTACK. Break the chairman's verdict if evidence permits.\n\n"
        f"Decision: {question}\nVERDICT: {verdict}\nNEXT: {next_step}\nRead {brief}.\n\n"
        f"Write to {output}. First lines: GIST: <strongest attack or survives> and "
        "SEVERITY: <FATAL|SERIOUS|MINOR|SURVIVES>. Then under 200 words give evidence "
        f"and a replacement if needed. Create {output}.done.\n\n{NO_CHAT}"
    )


def ruling_task(
    question: str,
    brief: Path,
    attack: Path,
    verdict: str,
    next_step: str,
    severity: str,
    output: Path,
) -> str:
    return (
        f"COUNCIL CHAIRMAN RULING. Reconsider your verdict on `{question}` after a "
        f"{severity} attack. Original VERDICT: {verdict}\nOriginal NEXT: {next_step}\n"
        f"Read {brief} and {attack}; verify claims.\n\nWrite to {output}. First four lines:\n"
        "RULING: <HOLD|REVISE>\nVERDICT: <full final verdict>\nNEXT: <one action>\n"
        "DISSENT: <strongest rejected point>\n\nThen under 250 words explain verified and "
        f"rejected attack claims. Create {output}.done.\n\n{NO_CHAT}"
    )


def _ready(path: Path, settle_s: float) -> bool:
    try:
        if path.with_suffix(path.suffix + ".done").exists():
            return path.exists() and path.stat().st_size > 0
        stat = path.stat()
        return stat.st_size > 0 and time.time() - stat.st_mtime >= settle_s
    except OSError:
        return False


def _collect(
    files: dict[str, Path],
    *,
    timeout_s: float,
    poll_s: float,
    settle_s: float,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[dict[str, str], list[str]]:
    deadline = time.time() + timeout_s
    pending = dict(files)
    results: dict[str, str] = {}
    while pending and time.time() < deadline:
        for seat, path in list(pending.items()):
            if not _ready(path, settle_s):
                continue
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text:
                results[seat] = text
                pending.pop(seat)
                if on_progress is not None:
                    on_progress(len(results), len(files))
        if pending:
            time.sleep(poll_s)
    return results, sorted(pending)


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("CLOVER_SESSION_") or key.startswith("CLOVER_TURN_"):
            env.pop(key, None)
    return env


def _dispatch(
    seat: str,
    task: str,
    *,
    roster: dict[str, dict[str, str]],
    clover_bin: str,
    work: Path,
    log_path: Path,
    usage_records: list[tuple[str, Path]],
) -> bool:
    usage = work / f"usage-{seat}-{time.time_ns()}.json"
    command = build_seat_command(seat, task, roster, clover_bin=clover_bin, usage_file=usage)
    try:
        with log_path.open("a", encoding="utf-8") as log:
            route = roster[seat]
            log.write(
                f"\n=== {time.strftime('%F %T')} seat={seat} "
                f"provider={route['provider']} model={route['model']} ===\n"
            )
            log.flush()
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                env=_subprocess_env(),
            )
        usage_records.append((seat, usage))
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"DISPATCH FAILED seat={seat}: {exc}\n")
        return False


def _append_report(report: Path, heading: str, text: str = "") -> None:
    with report.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {heading}\n")
        if text:
            handle.write(f"\n{text.rstrip()}\n")


def _validate_usage_records(
    records: list[tuple[str, Path]],
    roster: dict[str, dict[str, str]],
    expected_seats: set[str],
    *,
    timeout_s: float = 60,
) -> None:
    pending = list(records)
    deadline = time.time() + timeout_s
    while pending and time.time() < deadline:
        pending = [(seat, path) for seat, path in pending if not path.exists()]
        if pending:
            time.sleep(0.2)
    available = [(seat, path) for seat, path in records if path.exists()]
    available_seats = {seat for seat, _ in available}
    missing_seats = sorted(expected_seats - available_seats)
    if missing_seats:
        raise RuntimeError(
            "missing route metadata for returned council work: "
            + ", ".join(missing_seats)
        )
    for seat, path in available:
        validate_usage_route(seat, roster, path)


def write_final_report(
    report: Path,
    *,
    question: str,
    mode: str,
    verdict: str,
    next_step: str,
    dissent: str,
    chairman_text: str,
    elapsed_s: int,
    returned: int,
    expected: int,
    reviews: int,
) -> None:
    _append_report(
        report,
        "FINAL VERDICT",
        f"Question: {question}\nMode: {mode}\n\nVERDICT: {verdict}\nNEXT: {next_step}\n"
        f"DISSENT: {dissent}\n\nReturned: {returned}/{expected}; reviews: {reviews}; "
        f"elapsed: {elapsed_s}s\n\n### Chairman, full\n\n{chairman_text}",
    )


def run_council(
    question: str,
    mode: str,
    *,
    roster_path: Path = DEFAULT_ROSTER_PATH,
    run_id: str | None = None,
    home: Path | None = None,
    clover_bin: str | None = None,
    timeout_s: float = 1500,
    poll_s: float = 2,
    settle_s: float = 5,
) -> tuple[int, Path, dict[str, Any]]:
    started = time.time()
    home = home or clover_home()
    roster = load_roster(roster_path)
    clover_bin = clover_bin or os.environ.get("CLOVER_BIN") or shutil.which("clover")
    if not clover_bin:
        raise RuntimeError("Clover CLI not found; set CLOVER_BIN")
    run_id = run_id or f"council-{time.strftime('%Y%m%d-%H%M%S')}"
    work = home / "council" / "runs" / run_id
    work.mkdir(parents=True, exist_ok=False)
    report = work / "report.md"
    log_path = work / "agents.log"
    seats = MODE_SEATS[mode]
    usage_records: list[tuple[str, Path]] = []
    report.write_text(
        f"# COUNCIL {run_id}\n\nQuestion: {question}\nMode: {mode}\n"
        f"Seats: {', '.join(seats)}\n",
        encoding="utf-8",
    )
    progress_state = {
        "stage": "arguments",
        "seat_done": 0,
        "seat_total": len(seats),
        "review_done": 0,
        "review_total": 0 if mode == "quick" else len(seats),
        "stalled": [],
    }

    def publish(stage: str, status: str = "running", **updates: Any) -> None:
        progress_state["stage"] = stage
        progress_state.update(updates)
        write_progress(
            work,
            status=status,
            mode=mode,
            stage=stage,
            seat_done=progress_state["seat_done"],
            seat_total=progress_state["seat_total"],
            review_done=progress_state["review_done"],
            review_total=progress_state["review_total"],
            stalled=progress_state["stalled"],
            started_at=started,
        )

    publish("arguments")

    stage1_files = {seat: work / f"stage1-{seat}.md" for seat in seats}
    dispatched = [
        seat for seat in seats
        if _dispatch(
            seat,
            seat_task(seat, question, stage1_files[seat], clover_home=home),
            roster=roster,
            clover_bin=clover_bin,
            work=work,
            log_path=log_path,
            usage_records=usage_records,
        )
    ]
    answers, stalled1 = _collect(
        stage1_files,
        timeout_s=timeout_s,
        poll_s=poll_s,
        settle_s=settle_s,
        on_progress=lambda done, _total: publish("arguments", seat_done=done),
    )
    publish("arguments", seat_done=len(answers), stalled=stalled1)
    returned_route_seats = set(answers)
    for seat in seats:
        text = answers.get(seat, "STALLED — no answer")
        _append_report(report, f"STAGE 1 · {seat}", text)
    if not answers:
        _append_report(report, "RUN FAILED", "No council seat returned an answer.")
        publish("arguments", status="failed", stalled=stalled1)
        return 1, report, {"stage": "stage1", "stalled": stalled1, "dispatched": dispatched}

    reviews: dict[str, str] = {}
    stalled2: list[str] = []
    mapping: dict[str, str] = {}
    letters: list[str] = []
    if mode != "quick" and len(answers) >= 2:
        publish("cross_review", review_total=len(answers))
        order = list(answers)
        random.shuffle(order)
        letters = [chr(ord("A") + index) for index in range(len(order))]
        mapping = dict(zip(letters, order))
        anonymous = work / "anonymous.md"
        anonymous.write_text(
            "\n\n".join(
                f"## Response {letter}\n\n{scrub(answers[mapping[letter]])}"
                for letter in letters
            ),
            encoding="utf-8",
        )
        review_files = {seat: work / f"stage2-{seat}.md" for seat in answers}
        for seat, output in review_files.items():
            _dispatch(
                seat,
                review_task(seat, question, anonymous, output, letters),
                roster=roster,
                clover_bin=clover_bin,
                work=work,
                log_path=log_path,
                usage_records=usage_records,
            )
        reviews, stalled2 = _collect(
            review_files,
            timeout_s=timeout_s,
            poll_s=poll_s,
            settle_s=settle_s,
            on_progress=lambda done, _total: publish("cross_review", review_done=done),
        )
        publish(
            "cross_review",
            review_done=len(reviews),
            stalled=sorted(set(stalled1 + stalled2)),
        )
        returned_route_seats.update(reviews)
        _append_report(
            report,
            "ANONYMISATION MAP",
            "\n".join(f"Response {letter} = {mapping[letter]}" for letter in letters),
        )
        for seat, text in reviews.items():
            _append_report(report, f"STAGE 2 REVIEW · {seat}", text)

    brief = work / "chairman-brief.md"
    brief_parts = [f"# Decision\n\n{question}", "# Seat answers"]
    for seat in seats:
        brief_parts.append(f"## {seat}\n\n{answers.get(seat, '(stalled)')}")
    if reviews:
        brief_parts.append("# Anonymous cross-reviews")
        brief_parts.append("Letter map: " + ", ".join(f"{k}={v}" for k, v in mapping.items()))
        for seat, text in reviews.items():
            brief_parts.append(f"## Review by {seat}\n\n{text}")
    brief.write_text("\n\n".join(brief_parts), encoding="utf-8")

    chairman_output = work / "stage3-CHAIRMAN.md"
    publish("chairman", stalled=sorted(set(stalled1 + stalled2)))
    _dispatch(
        "CHAIRMAN",
        chairman_task(question, brief, chairman_output, mode, stalled1 + stalled2),
        roster=roster,
        clover_bin=clover_bin,
        work=work,
        log_path=log_path,
        usage_records=usage_records,
    )
    chair_results, chair_stalled = _collect(
        {"CHAIRMAN": chairman_output},
        timeout_s=timeout_s,
        poll_s=poll_s,
        settle_s=settle_s,
    )
    if chair_stalled:
        _append_report(report, "RUN FAILED", "Chairman did not return a verdict.")
        publish("chairman", status="failed")
        return 1, report, {"stage": "chairman", "stalled": chair_stalled}
    chairman_text = chair_results["CHAIRMAN"]
    returned_route_seats.add("CHAIRMAN")
    verdict, next_step, dissent = parse_verdict(chairman_text)
    if not verdict or not next_step or not dissent:
        _append_report(report, "RUN FAILED", "Chairman output was not parseable.\n\n" + chairman_text)
        publish("chairman", status="failed")
        return 1, report, {"stage": "chairman-parse", "stalled": []}

    attack_severity = ""
    ruling = ""
    if mode == "deep":
        publish("attack")
        attack_output = work / "stage4-ATTACK.md"
        _dispatch(
            "ATTACK",
            attack_task(question, brief, verdict, next_step, attack_output),
            roster=roster,
            clover_bin=clover_bin,
            work=work,
            log_path=log_path,
            usage_records=usage_records,
        )
        attack_results, _ = _collect(
            {"ATTACK": attack_output},
            timeout_s=timeout_s,
            poll_s=poll_s,
            settle_s=settle_s,
        )
        if attack_results:
            returned_route_seats.add("ATTACK")
            attack_text = attack_results["ATTACK"]
            attack_severity = parse_severity(attack_text)
            _append_report(report, f"STAGE 4 ATTACK · {attack_severity}", attack_text)
            if attack_severity in {"FATAL", "SERIOUS"}:
                publish("ruling")
                ruling_output = work / "stage5-CHAIRMAN.md"
                _dispatch(
                    "CHAIRMAN",
                    ruling_task(
                        question, brief, attack_output, verdict, next_step,
                        attack_severity, ruling_output,
                    ),
                    roster=roster,
                    clover_bin=clover_bin,
                    work=work,
                    log_path=log_path,
                    usage_records=usage_records,
                )
                ruling_results, _ = _collect(
                    {"CHAIRMAN": ruling_output},
                    timeout_s=timeout_s,
                    poll_s=poll_s,
                    settle_s=settle_s,
                )
                if ruling_results:
                    returned_route_seats.add("CHAIRMAN")
                    ruling_text = ruling_results["CHAIRMAN"]
                    revised_verdict, revised_next, revised_dissent = parse_verdict(ruling_text)
                    ruling_match = re.search(
                        r"^\**\s*RULING\s*:\**\s*(HOLD|REVISE)", ruling_text, re.I | re.M
                    )
                    ruling = ruling_match.group(1).upper() if ruling_match else "UNPARSEABLE"
                    if revised_verdict:
                        verdict = revised_verdict
                        next_step = revised_next or next_step
                        dissent = revised_dissent or dissent
                    _append_report(report, f"STAGE 5 RULING · {ruling}", ruling_text)

    try:
        _validate_usage_records(usage_records, roster, returned_route_seats)
    except RuntimeError as exc:
        _append_report(report, "RUN FAILED", str(exc))
        publish(str(progress_state["stage"]), status="failed")
        return 1, report, {"stage": "route-verification", "error": str(exc)}

    elapsed = int(time.time() - started)
    write_final_report(
        report,
        question=question,
        mode=mode,
        verdict=verdict,
        next_step=next_step,
        dissent=dissent,
        chairman_text=chairman_text,
        elapsed_s=elapsed,
        returned=len(answers),
        expected=len(seats),
        reviews=len(reviews),
    )
    summary = {
        "run_id": run_id,
        "mode": mode,
        "verdict": verdict,
        "next": next_step,
        "dissent": dissent,
        "stalled": sorted(set(stalled1 + stalled2)),
        "attack_severity": attack_severity,
        "ruling": ruling,
        "elapsed_s": elapsed,
    }
    (work / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    publish("ruling" if ruling else "attack" if attack_severity else "chairman", status="done")
    return 0, report, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the adversarial Clover council")
    parser.add_argument("question")
    parser.add_argument("--mode", choices=sorted(MODE_SEATS), default="full")
    parser.add_argument("--id", default=None)
    parser.add_argument("--models", type=Path, default=DEFAULT_ROSTER_PATH)
    parser.add_argument("--timeout", type=float, default=1500)
    args = parser.parse_args()
    if not args.question.strip():
        parser.error("question cannot be empty")
    code, report, summary = run_council(
        args.question.strip(),
        args.mode,
        roster_path=args.models,
        run_id=args.id,
        timeout_s=args.timeout,
    )
    if code:
        print(f"COUNCIL_FAILED stage={summary.get('stage', 'unknown')}")
    else:
        print(f"VERDICT: {summary['verdict']}")
        print(f"NEXT: {summary['next']}")
        print(f"DISSENT: {summary['dissent']}")
        if summary["stalled"]:
            print("STALLED: " + ", ".join(summary["stalled"]))
        if summary["attack_severity"]:
            print(f"ATTACK: {summary['attack_severity']} {summary['ruling']}")
    print(f"COUNCIL_REPORT={report.resolve()}")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
