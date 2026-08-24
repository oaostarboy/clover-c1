#!/usr/bin/env python3
"""Preflight: is this agent actually going to work?

Written after a night where Clover sat silent for hours and every individual
component looked fine. Config on disk is a REQUEST. This checks the things that
were actually true at the moment of failure:

  * a fallback chain that PARSES (four entries configured, zero parsed, no
    warning anywhere — that was the outage)
  * a primary model that ANSWERS (one agent's xai/grok-4.5 returns 401)
  * memory that fits under its own ceiling (import 20k into a 2.2k budget and
    every future write is silently refused)
  * skills that belong to THIS agent (a migration once handed a security agent
    464 of the operator's marketing skills)
  * an identity that survived the move

Exit code is the point: 0 = go, 1 = do not go. Safe to run repeatedly, makes
no changes, and never prints a secret.

    preflight.py --home ~/.clover
    preflight.py --home ~/.clover --probe     # also spend a token on a live turn
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_ICON = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))

    def render(self) -> int:
        for status, name, detail in self.rows:
            line = f"[{_ICON[status]}] {name}"
            if detail:
                line += f"\n           {detail}"
            print(line)
        fails = sum(1 for s, _, _ in self.rows if s == FAIL)
        warns = sum(1 for s, _, _ in self.rows if s == WARN)
        print()
        if fails:
            print(f"=== {fails} FAILURE(S) — do not put this agent in front of anyone ===")
            return 1
        if warns:
            print(f"=== clean, with {warns} warning(s) ===")
            return 0
        print("=== all checks pass ===")
        return 0


def load_config(home: Path) -> dict:
    cfg_path = home / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml
        return yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:
        return {}


def check_fallback_chain(home: Path, cfg: dict, rep: Report, repo: Path) -> None:
    """The one that mattered. Configured != parsed."""
    raw = cfg.get("fallback_providers") or []
    if not raw:
        rep.add(WARN, "fallback chain",
                "No fallbacks configured. A single-model agent goes silent the "
                "moment that model is unavailable.")
        return
    sys.path.insert(0, str(repo))
    try:
        from clover_cli.fallback_config import get_fallback_chain
        parsed = get_fallback_chain(cfg)
    except Exception as exc:
        rep.add(WARN, "fallback chain", f"could not verify: {exc}")
        return
    if len(parsed) != len(raw):
        rep.add(FAIL, "fallback chain",
                f"{len(raw)} configured but only {len(parsed)} PARSE. The "
                "difference is silently unusable — this is exactly how the "
                "agent goes quiet under load.")
        return
    # PARSING IS NOT RESOLVING. "claude-cli/claude-opus-5" parses perfectly:
    # provider "claude-cli", model "claude-opus-5". But claude-cli is an
    # OPENCLAW provider id. Clover has never heard of it, so the entry is a
    # well-formed pointer to nothing.
    #
    # Caught live 2026-08-24: Alfred passed this check with "7 usable" and then
    # could not answer at all — every one of his seven fallbacks was a
    # claude-cli/ or claude-proxy/ name carried verbatim from OpenClaw. A gate
    # that says OK while the agent cannot speak is worse than no gate.
    known = set(_known_providers(cfg, repo))
    unknown = [f"{p['provider']}/{p['model']}" for p in parsed
               if p["provider"] not in known]
    if unknown:
        rep.add(FAIL, "fallback chain",
                f"{len(unknown)} of {len(parsed)} name a provider Clover cannot "
                f"resolve: {', '.join(unknown[:3])}"
                + (" ..." if len(unknown) > 3 else "")
                + ". These parse but point at nothing. OpenClaw ids such as "
                "claude-cli/ and claude-proxy/ do not exist here — the Claude "
                "Code CLI route is provider 'anthropic'.")
        return
    names = ", ".join(f"{p['provider']}/{p['model']}" for p in parsed[:3])
    rep.add(OK, "fallback chain", f"{len(parsed)} usable: {names}"
            + (" ..." if len(parsed) > 3 else ""))


def _known_providers(cfg: dict, repo: Path) -> set:
    """Providers this install can actually route to.

    Built-ins from Clover's own registry, plus whatever custom_providers the
    agent defines. Anything else is a dead name.
    """
    names = set()
    for entry in (cfg.get("custom_providers") or []):
        if isinstance(entry, dict) and entry.get("name"):
            names.add(str(entry["name"]).strip())
    try:
        sys.path.insert(0, str(repo))
        from clover_cli.providers import ALIASES  # noqa: E402
        names.update(ALIASES.keys())
        names.update(ALIASES.values())
        try:
            from clover_cli.provider_catalog import PROVIDERS as _CAT  # noqa: E402
            names.update(_CAT.keys())
        except Exception:
            pass
    except Exception:
        # Fail OPEN on the registry import, not closed: better to miss a bad
        # name than to block a good migration on an import error.
        names.update({"anthropic", "openai", "openrouter", "ollama", "google"})
    return names


def check_memory_budget(home: Path, cfg: dict, rep: Report) -> None:
    mem_file = home / "memories" / "MEMORY.md"
    if not mem_file.exists():
        rep.add(WARN, "memory", "no memories/MEMORY.md — agent starts blank")
        return
    size = len(mem_file.read_text(errors="replace"))
    limit = int((cfg.get("memory") or {}).get("memory_char_limit") or 2200)
    if size > limit:
        rep.add(FAIL, "memory budget",
                f"MEMORY.md is {size:,} chars but memory_char_limit is "
                f"{limit:,}. Every future memory write will be refused. Raise "
                "the limit or consolidate.")
    else:
        head = round(100 * size / limit)
        rep.add(OK, "memory budget", f"{size:,} / {limit:,} chars ({head}% used)")


def check_identity(home: Path, rep: Report) -> None:
    soul = home / "SOUL.md"
    if not soul.exists():
        rep.add(FAIL, "identity", "no SOUL.md — the agent has no persona")
        return
    text = soul.read_text(errors="replace")
    if len(text.strip()) < 200:
        rep.add(WARN, "identity", f"SOUL.md is only {len(text)} chars — looks like a stub")
        return
    first = next((l for l in text.splitlines() if l.strip()), "")
    rep.add(OK, "identity", f"SOUL.md {len(text):,} chars — {first[:60]}")


def check_skills(home: Path, rep: Report) -> None:
    skills_dir = home / "skills"
    if not skills_dir.is_dir():
        rep.add(WARN, "skills", "no skills directory")
        return
    found = list(skills_dir.rglob("SKILL.md"))
    if not found:
        rep.add(WARN, "skills", "skills directory is empty")
        return
    names = sorted(p.parent.name for p in found)
    rep.add(OK, "skills", f"{len(found)} present: " + ", ".join(names[:5])
            + (" ..." if len(names) > 5 else ""))


def check_primary_model(cfg: dict, rep: Report, probe: bool) -> None:
    model = (cfg.get("model") or {})
    name = model.get("default") if isinstance(model, dict) else model
    if not name:
        rep.add(FAIL, "primary model", "no model configured")
        return
    if not probe:
        rep.add(OK, "primary model", f"{name} (not probed; pass --probe to spend a live turn)")
        return
    provider = str(name).split("/")[0]
    if provider in ("claude-cli", "anthropic"):
        slug = str(name).split("/", 1)[-1]
        try:
            r = subprocess.run(["claude", "--model", slug, "-p", "say ok"],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and r.stdout.strip():
                rep.add(OK, "primary model", f"{name} answered")
            else:
                rep.add(FAIL, "primary model",
                        f"{name} did not answer: {(r.stdout + r.stderr).strip()[:90]}")
        except Exception as exc:
            rep.add(FAIL, "primary model", f"{name} probe failed: {exc}")
    else:
        # Every other provider: probe the HTTP endpoint from the config. Not a
        # completion, but it separates "credentials are dead" from "fine".
        # Written because that agent's primary (xai/grok-4.5) returns 401 while
        # preflight cheerfully reported "all checks pass" — a check that only
        # examines the providers it happens to understand is worse than no
        # check, because it grants confidence it did not earn.
        base = ""
        for entry in (cfg.get("custom_providers") or []):
            if isinstance(entry, dict) and entry.get("name") == provider:
                base = str(entry.get("base_url") or "").rstrip("/")
                break
        if not base:
            rep.add(WARN, "primary model",
                    f"{name} — provider '{provider}' has no base_url in config; "
                    "cannot verify. Check it by hand before trusting this agent.")
            return
        # base_url may or may not already end in /v1. Probing "<base>/models"
        # against a bare host gives a 404 from a perfectly healthy server —
        # measured against Alfred's local ollama, which answers 200 on
        # /v1/models and /api/tags but 404 on /models. A false alarm here costs
        # exactly as much trust as a missed one.
        candidates = []
        if base.endswith("/v1"):
            candidates.append(f"{base}/models")
        else:
            candidates.append(f"{base}/v1/models")
            candidates.append(f"{base}/api/tags")
        url = candidates[0]
        code = ""
        for url in candidates:
            try:
                r = subprocess.run(
                    ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     "--max-time", "15", url],
                    capture_output=True, text=True, timeout=25,
                )
                code = (r.stdout or "").strip()
            except Exception as exc:
                rep.add(WARN, "primary model", f"{name} — probe failed: {exc}")
                return
            if code in ("200", "204", "401", "403"):
                break   # a definitive answer; stop probing
        if code in ("200", "204"):
            rep.add(OK, "primary model", f"{name} — {provider} reachable (HTTP {code})")
        elif code in ("401", "403"):
            rep.add(FAIL, "primary model",
                    f"{name} — {provider} rejects the stored credential "
                    f"(HTTP {code}). This agent's PRIMARY model is dead; it will "
                    "run entirely on its fallback chain. Re-authenticate.")
        elif code == "000":
            rep.add(FAIL, "primary model",
                    f"{name} — {provider} at {base} is unreachable.")
        else:
            rep.add(WARN, "primary model", f"{name} — {provider} returned HTTP {code}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", required=True, help="Clover home (e.g. ~/.clover)")
    ap.add_argument("--probe", action="store_true", help="spend a real turn on the primary model")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[4]))
    a = ap.parse_args()

    home = Path(a.home).expanduser()
    if not home.is_dir():
        print(f"not a directory: {home}", file=sys.stderr)
        return 2

    cfg = load_config(home)
    rep = Report()
    print(f"preflight: {home}\n")
    check_identity(home, rep)
    check_memory_budget(home, cfg, rep)
    check_skills(home, rep)
    check_primary_model(cfg, rep, a.probe)
    check_fallback_chain(home, cfg, rep, Path(a.repo))
    return rep.render()


if __name__ == "__main__":
    sys.exit(main())
