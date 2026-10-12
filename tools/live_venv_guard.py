"""Never let a delegated child install into a live agent's virtualenv.

On 2026-10-12 a subagent's test run re-pointed the LIVE gateway venv's
editable install at the subagent's own worktree, then the worktree was
deleted; the gateway crash-looped 57 times on ``No module named
'clover_cli'``. This module is the floor that stops a child agent's
terminal command from installing into:

- the running process's own venv (``sys.prefix`` when it is a venv), and
- any ``<agent-root>/*/venv`` / ``.venv`` (``~/agents`` by default; see
  ``delegation.foreign_agent_roots``).

It is used by ``tools.approval`` as a hardline-style block that only fires
for delegated children; the parent agent's own maintenance is unaffected.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from typing import List, Optional

_ADVICE = (
    "Make your own venv in your worktree instead: `uv venv .venv && "
    "uv pip install --python .venv/bin/python -e .` (or run tests with the "
    "existing python and no install)."
)


def _real(path: str) -> str:
    try:
        return os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    except Exception:
        return path


def protected_venvs() -> List[str]:
    """Real paths of venvs a child must never install into."""
    out: list = []
    if getattr(sys, "prefix", None) and sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        out.append(_real(sys.prefix))
    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env:
        out.append(_real(venv_env))
    try:
        from tools.subagent_worktree import foreign_agent_roots

        roots = foreign_agent_roots()
    except Exception:
        roots = [_real("~/agents")]
    for root in roots:
        try:
            for name in os.listdir(root):
                for sub in ("venv", ".venv"):
                    cand = os.path.join(root, name, sub)
                    if os.path.isdir(cand):
                        out.append(_real(cand))
        except OSError:
            continue
    seen: list = []
    for p in out:
        if p and p not in seen:
            seen.append(p)
    return seen


def _inside(path: str, venvs: List[str]) -> Optional[str]:
    rp = _real(path)
    for v in venvs:
        if rp == v or rp.startswith(v.rstrip(os.sep) + os.sep):
            return v
    return None


_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n|\(|\)")
_ENV_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _segments(command: str) -> List[List[str]]:
    segs = []
    for raw in _SEGMENT_SPLIT.split(command or ""):
        raw = raw.strip()
        if not raw:
            continue
        try:
            words = shlex.split(raw, posix=True)
        except ValueError:
            words = raw.split()
        if words:
            segs.append(words)
    return segs


def _flag_value(words: List[str], *flags: str) -> Optional[str]:
    for i, w in enumerate(words):
        for f in flags:
            if w == f and i + 1 < len(words):
                return words[i + 1]
            if w.startswith(f + "="):
                return w.split("=", 1)[1]
    return None


def detect_live_venv_install(command: str, venvs: Optional[List[str]] = None) -> Optional[str]:
    """Return a reason when *command* would install into a protected venv."""
    venvs = protected_venvs() if venvs is None else venvs
    if not venvs or not command:
        return None
    activated: Optional[str] = None  # venv made active earlier in the command
    for words in _segments(command):
        env: dict = {}
        if words and words[0] == "env":
            words = words[1:]
        while words:
            m = _ENV_ASSIGN.match(words[0])
            if not m:
                break
            env[m.group(1)] = m.group(2)
            words = words[1:]
        if not words:
            if "VIRTUAL_ENV" in env:
                activated = env["VIRTUAL_ENV"]
            continue
        if words[0] in ("export",) and len(words) > 1:
            m = _ENV_ASSIGN.match(words[1])
            if m and m.group(1) == "VIRTUAL_ENV":
                activated = m.group(2)
            continue
        if words[0] in ("source", ".") and len(words) > 1 and words[1].endswith("/activate"):
            activated = os.path.dirname(os.path.dirname(words[1]))
            continue
        exe = words[0]
        base = os.path.basename(exe)
        rest = words[1:]
        target_venv = env.get("VIRTUAL_ENV") or activated

        # uv pip install / uv sync
        if base in ("uv", "uv.exe"):
            if rest[:2] == ["pip", "install"] or rest[:1] == ["sync"] or rest[:2] == ["pip", "sync"]:
                py = _flag_value(rest, "--python", "-p")
                if py and _inside(py, venvs):
                    return f"uv install targets a live agent venv via --python {py}"
                prefix = _flag_value(rest, "--prefix", "--target")
                if prefix and _inside(prefix, venvs):
                    return f"uv install targets a live agent venv via {prefix}"
                proj_env = env.get("UV_PROJECT_ENVIRONMENT")
                if proj_env and _inside(proj_env, venvs):
                    return f"uv sync targets a live agent venv via UV_PROJECT_ENVIRONMENT={proj_env}"
                if target_venv and _inside(target_venv, venvs) and (
                    rest[:1] == ["pip"] or "--active" in rest
                ):
                    return f"uv install targets a live agent venv via VIRTUAL_ENV={target_venv}"
                if "--system" in rest and _inside(sys.prefix, venvs):
                    return "uv --system install targets this agent's live interpreter"
            continue

        # python -m pip install / pip install
        pip_install = False
        interp: str = exe
        if base.startswith("python") and rest[:3] == ["-m", "pip", "install"]:
            pip_install = True
        elif base.startswith("python") and rest[:2] == ["-m", "pip"] and "install" in rest:
            pip_install = True
        elif re.fullmatch(r"pip(3(\.\d+)?)?(\.exe)?", base) and rest[:1] == ["install"]:
            pip_install = True
        if not pip_install:
            continue
        if os.sep in interp or interp.startswith("~"):
            hit = _inside(interp, venvs)
            if hit:
                return f"pip install runs with a live agent venv's interpreter ({interp})"
            continue
        # Bare `pip`/`python` resolves through PATH; in a child shell the live
        # gateway venv's bin dir is first unless a venv was activated here.
        if target_venv and not _inside(target_venv, venvs):
            continue
        return (
            f"bare `{base}` install resolves to the live agent venv on PATH "
            f"({venvs[0]})"
        )
    return None


def live_venv_block_result(reason: str) -> dict:
    return {
        "approved": False,
        "hardline": True,
        "message": (
            f"BLOCKED (hardline): {reason}. Subagents must never install into "
            "a live agent's virtualenv — doing so has crash-looped a gateway. "
            + _ADVICE
        ),
    }
