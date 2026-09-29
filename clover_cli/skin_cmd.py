"""``clover skin`` — list, switch, and tweak skins from the CLI.

``set`` is the load-bearing verb: it changes ONE color of the ACTIVE skin **in
place**, so tweaking (say) the tool marker never disturbs the rest of the look —
background included. Editing the file bumps its mtime; the gateway's skin watcher
repaints every live surface within ~a second. A built-in skin (no file) is forked
into an editable copy that carries its full palette, so the current look is
preserved and only the one key changes.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from clover_constants import display_clover_home, get_clover_home

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _skins_dir() -> Path:
    return get_clover_home() / "skins"


def _active_skin() -> str:
    from clover_cli.config import load_config
    from clover_cli.skin_engine import DEFAULT_SKIN_NAME

    display = (load_config() or {}).get("display") or {}
    return str(display.get("skin") or DEFAULT_SKIN_NAME)


def _use(name: str) -> None:
    """Activate a skin (persists display.skin via the shared config writer)."""
    from clover_cli.config import config_command

    config_command(argparse.Namespace(config_command="set", key="display.skin", value=name, force=True))


def _skin_set(key: str, value: str, skin: str | None) -> int:
    import yaml

    if not _HEX_RE.match(value):
        print(f"✗ {value!r} is not a #rrggbb hex color", file=sys.stderr)
        return 1

    name = skin or _active_skin()
    path = _skins_dir() / f"{name}.yaml"

    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        target = name
    else:
        # Built-in (or missing): fork into an editable copy that keeps its full
        # palette, under a fresh name so the built-in stays intact for revert.
        from clover_cli.skin_engine import load_skin

        resolved = load_skin(name)
        target = f"{name}-custom"
        path = _skins_dir() / f"{target}.yaml"
        data = {
            "name": target,
            "description": f"{name} + custom {key}",
            "colors": dict(resolved.colors),
            "branding": dict(resolved.branding),
            "tool_prefix": resolved.tool_prefix,
        }

    if not isinstance(data.get("colors"), dict):
        data["colors"] = {}
    data["colors"][key] = value
    data.setdefault("name", target)

    # Atomic write: write_text truncates with no fsync; safe_load("") → None
    # → {} would permanently lose the palette on the next set (#51356, #16743).
    from utils import atomic_yaml_write

    atomic_yaml_write(path, data, sort_keys=False)

    if target != name:
        _use(target)

    print(f"✓ {key} = {value} in {display_clover_home()}/skins/{target}.yaml (live within ~1s)")
    return 0


def _skin_list() -> int:
    from clover_cli.skin_engine import list_skins

    active = _active_skin()
    for s in list_skins():
        mark = "*" if s["name"] == active else " "
        print(f"{mark} {s['name']:<16} {s.get('source', ''):<8} {s.get('description', '')}")
    return 0


_PREVIEW_KINDS = (
    ("restarting", "restarting"),
    ("back_online", "back online"),
    ("busy", "busy"),
    ("stop", "stop"),
    ("memory", "learned something"),
)


def skin_preview(name: str) -> str:
    """Five sample lines (restarting, back online, busy, stop, learned-something) as *name* words them."""
    import random
    import threading

    from agent import clover_flavor
    from clover_cli.skin_engine import load_skin

    pack = clover_flavor.pack_for_skin(load_skin(name))
    rows = [f"Preview: {name}"]
    for kind, label in _PREVIEW_KINDS:
        if clover_flavor.has_lines(pack, kind):
            done = kind in ("back_online", "memory")
            face, line = clover_flavor.pick_pair(
                pack, kind, {}, threading.Lock(), "preview", random.Random(0),
                mark=pack["done_mark"] if done else None,
            )
            rows.append(f"• {label}: {clover_flavor._join(face, line)}")
        else:
            rows.append(f"• {label}: (stock wording)")
    return "\n".join(rows)


def apply_skin(name: str, save) -> str:
    """Switch to skin *name* now and persist it with *save(key, value) -> bool*; returns the reply."""
    from clover_cli.skin_engine import set_active_skin

    set_active_skin(name)
    if save("display.skin", name):
        return f"Skin set to {name}."
    return f"Skin set to {name} for now, but I couldn't save it to config.yaml."


def resolve_skin_arg(arg: str):
    """The skin a ``/skin <arg>`` names (a name, or a 1-based number from the list); None if unknown."""
    from clover_cli.skin_engine import list_skins

    names = [s["name"] for s in list_skins()]
    arg = arg.strip()
    if arg.isdigit() and 1 <= int(arg) <= len(names):
        return names[int(arg) - 1]
    lowered = {n.lower(): n for n in names}
    return lowered.get(arg.lower())


def skin_command(args) -> None:
    """Dispatch ``clover skin <verb>``."""
    verb = getattr(args, "skin_command", None)

    if verb == "set":
        sys.exit(_skin_set(args.key, args.value, getattr(args, "skin", None)))
    elif verb == "use":
        _use(args.name)
        print(f"✓ active skin → {args.name} (live within ~1s)")
    else:  # list / default
        sys.exit(_skin_list())
