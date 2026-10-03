"""Durable proof for Linux/systemd catch-up gateway restarts.

This is deliberately separate from general update receipt outcomes. Its tag is
written only for an already-current checkout that owes a fleet restart.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_TAG = "linux_systemd_catchup"
_SHA = re.compile(r"^[0-9a-f]{40,64}$")


def _supported() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    from clover_cli.gateway import supports_systemd_services
    return bool(supports_systemd_services())


def prepare_pending_receipt(
    receipt_api, *, clover_home: Path, pre_restart_pids: set[int]
) -> bool:
    """Persist a fresh running receipt with known profile/PID restart targets."""
    try:
        if not _supported() or receipt_api._current is None:
            return False
        from clover_cli.build_info import get_code_identity
        identity = get_code_identity(refresh=True) or {}
        expected_sha = str(identity.get("sha") or "")
        if not _SHA.fullmatch(expected_sha):
            return False
        if not pre_restart_pids:
            return False
        # Join positively owned systemd MainPIDs to this install's profile
        # inventory. Rows from other installations, and services whose PID
        # cannot be mapped to a current profile runtime, are not proof.
        fleet = receipt_api.collect_fleet_versions(pre_restart_pids=sorted(pre_restart_pids))
        targets = []
        seen = set()
        joined_pids = set()
        for row in fleet:
            profile, pid = row.get("profile"), row.get("pid")
            if pid not in pre_restart_pids:
                continue
            if not isinstance(profile, str) or not profile or not isinstance(pid, int) or pid <= 0:
                return False
            if profile in seen:
                return False
            seen.add(profile)
            joined_pids.add(pid)
            targets.append({"profile": profile, "pid": pid})
        if not targets or joined_pids != set(pre_restart_pids):
            return False
        prior = receipt_api._current.data.get(_TAG)
        if isinstance(prior, dict):
            # A retry may see only a subset of the original services. A down
            # profile remains owed a restart; never narrow the durable intent.
            prior_profiles = {item["profile"] for item in prior.get("targets", [])}
            if not prior_profiles.issubset(seen):
                return False
        # Preserve explicit current-home identity; never substitute a profile
        # home or adopt a receipt written for another installation.
        receipt_api._current.data[_TAG] = {
            "version": 1, "clover_home": str(clover_home.resolve()),
            "expected_sha": expected_sha, "targets": targets,
        }
        receipt_api.save_pending_receipt(expected_sha, None)
        return True
    except Exception:
        return False


def verify_and_finalize_receipt(receipt_path: Path, *, clover_home: Path, fleet: list[dict[str, Any]]) -> bool:
    """Finalize a tagged receipt only when every intended profile is a new live PID."""
    try:
        record = json.loads(receipt_path.read_text(encoding="utf-8"))
        original_started_at = record.get("started_at")
        original_pid = record.get("pid")
        if record.get("outcome") not in {"running", "success"}:
            return False
        tag = record.get(_TAG)
        if not isinstance(tag, dict) or tag.get("version") != 1:
            return False
        expected_home = str(clover_home.resolve())
        if tag.get("clover_home") != expected_home:
            return False
        sha, targets = tag.get("expected_sha"), tag.get("targets")
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            return False
        if not isinstance(targets, list) or not targets:
            return False
        expected = {}
        for item in targets:
            if not isinstance(item, dict):
                return False
            profile, pid = item.get("profile"), item.get("pid")
            if not isinstance(profile, str) or not profile or not isinstance(pid, int) or pid <= 0 or profile in expected:
                return False
            expected[profile] = pid
        live = {row.get("profile"): row for row in fleet if isinstance(row, dict)}
        for profile, old_pid in expected.items():
            row = live.get(profile)
            if (not row or row.get("state") != "current" or
                    row.get("code_sha") != sha or not isinstance(row.get("pid"), int) or
                    row["pid"] <= 0 or row["pid"] == old_pid):
                return False
        record["outcome"] = "success"
        record[_TAG] = dict(tag, verified=True)
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        record.setdefault("post_update", {})["sha"] = sha
        record["post_update"]["short_sha"] = sha[:12]
        # A later update can replace latest.json while verification is in
        # progress. Never publish this run's result over a different receipt.
        latest = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (
            latest.get("started_at") != original_started_at
            or latest.get("pid") != original_pid
            or latest.get(_TAG) != tag
            or latest.get("outcome") not in {"running", "success"}
        ):
            return False
        tmp = receipt_path.with_name(f"{receipt_path.name}.{os.getpid()}.tmp")
        marker = clover_home / "fleet_restart_pending"
        try:
            tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
            marker_bytes = marker.read_bytes() if marker.exists() else None
            marker.unlink(missing_ok=True)
            try:
                tmp.replace(receipt_path)
            except Exception:
                # The success receipt is still provisional: reinstate the
                # restart obligation if publishing it fails after marker clear.
                try:
                    marker.write_bytes(marker_bytes if marker_bytes is not None else
                                       f"expected_sha={sha}".encode("utf-8"))
                except OSError:
                    pass
                return False
            return True
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        return False


def _read_pending_tag(receipt_path: Path, *, clover_home: Path) -> dict | None:
    """Read a validated unresolved intent belonging to this installation."""
    try:
        if not _supported():
            return None
        record = json.loads(receipt_path.read_text(encoding="utf-8"))
        if record.get("outcome") not in {"running", "failed", "partial", "refused"}:
            return None
        tag = record.get(_TAG)
        if not isinstance(tag, dict) or tag.get("version") != 1:
            return None
        if tag.get("clover_home") != str(clover_home.resolve()):
            return None
        sha, targets = tag.get("expected_sha"), tag.get("targets")
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            return None
        if not isinstance(targets, list) or not targets:
            return None
        if not all(
            isinstance(item, dict) and isinstance(item.get("profile"), str)
            and bool(item["profile"]) and type(item.get("pid")) is int
            and item["pid"] > 0
            for item in targets
        ):
            return None
        return tag
    except Exception:
        return None


def inherit_pending_receipt(data: dict, *, clover_home: Path) -> None:
    """Carry unresolved targets into the new command's eventual receipt."""
    tag = _read_pending_tag(
        clover_home / "logs" / "update_receipts" / "latest.json", clover_home=clover_home,
    )
    if tag is not None:
        data[_TAG] = tag


def pending_receipt_requires_catchup(receipt_path: Path, *, clover_home: Path) -> bool:
    """The intent survives marker loss and failed command-boundary writes."""
    return _read_pending_tag(receipt_path, clover_home=clover_home) is not None
