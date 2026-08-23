#!/bin/sh
# shellcheck shell=sh
# /opt/clover/bin/clover — `docker exec` privilege-drop shim.
#
# Background
# ----------
# The s6 image runs the supervised gateway/main process as the unprivileged
# `clover` user (UID 10000). When an operator runs `docker exec <c> clover ...`
# the default UID is root (0), and any file the command writes under
# $CLOVER_HOME — auth.json, .env, config.yaml — ends up root-owned and
# unreadable to the supervised gateway. The most common manifestation: the
# user runs `docker exec <c> clover login`, this writes
# /opt/data/auth.json as root:root mode 0600, and from then on the gateway
# returns "Provider authentication failed: Clover is not logged into Clover
# Portal" on every incoming message — even though `docker exec <c> clover
# chat -q ping` (also running as root) succeeds because root happens to be
# able to read its own root-owned file. See systematic-debugging skill
# notes attached to this fix.
#
# Fix
# ---
# This shim sits at /opt/clover/bin/clover and is placed earliest on PATH.
# When invoked as root, it drops to the clover user (via s6-setuidgid)
# before exec'ing the real venv binary, so anything that writes under
# $CLOVER_HOME is uid-aligned with the supervised processes. When invoked
# as any non-root UID — including the supervised processes themselves,
# `docker exec --user clover`, kanban subagents, etc. — it short-circuits
# straight to the venv binary with no privilege change. Net: one extra
# fork on the docker-exec-as-root path, zero behavioral change on every
# other path.
#
# Recursion safety: the shim exec's the venv binary by *absolute path*
# (/opt/clover/.venv/bin/clover), so the second hop cannot re-enter this
# shim regardless of PATH state. No sentinel env var needed.
#
# Opt-out: set CLOVER_DOCKER_EXEC_AS_ROOT=1 (1/true/yes, case-insensitive)
# to keep running as root. Reserved for diagnostic sessions where the
# operator deliberately wants root semantics — e.g. inspecting root-only
# state via the clover CLI. Default is to drop.

set -e

REAL=/opt/clover/.venv/bin/clover

# Defensive: if the venv binary is missing (corrupted image, partial
# install), fail loudly rather than silently masking it.
if [ ! -x "$REAL" ]; then
    echo "clover-shim: $REAL not found or not executable" >&2
    exit 127
fi

# Already non-root? Just exec the real binary. This is the hot path for
# supervised processes (uid 10000) and for `docker exec --user clover`.
if [ "$(id -u)" != "0" ]; then
    exec "$REAL" "$@"
fi

# Root, with opt-out set? Honor it.
case "${CLOVER_DOCKER_EXEC_AS_ROOT:-}" in
    1|true|TRUE|True|yes|YES|Yes)
        exec "$REAL" "$@"
        ;;
esac

# Root, no opt-out. Drop to the clover user.
#
# s6-setuidgid lives under /command/ which is NOT on `docker exec`'s PATH
# (s6-overlay only puts /command/ on PATH for supervision-tree children).
# Reference it by absolute path so the drop is robust against PATH
# manipulation.
S6_SUID=/command/s6-setuidgid
if [ ! -x "$S6_SUID" ]; then
    # Non-s6 image (someone stripped s6-overlay, or a hand-built variant).
    # Fail loud rather than silently re-execing as root and leaking the
    # bug this shim exists to prevent.
    echo "clover-shim: $S6_SUID not found; refusing to silently run as root." >&2
    echo "clover-shim: re-run with --user clover or set CLOVER_DOCKER_EXEC_AS_ROOT=1." >&2
    exit 126
fi

# Reset HOME to the clover user's home before dropping privileges. Without
# this, $HOME stays /root and any library that resolves paths off $HOME
# (XDG caches, lockfiles, .config writes) will try to write to /root and
# fail with EACCES. Mirrors main-wrapper.sh.
export HOME=/opt/data

exec "$S6_SUID" clover "$REAL" "$@"
