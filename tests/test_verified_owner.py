"""Local-operator + transport proof, separate from chat admission and slash gating."""
import json

from gateway import pairing


def store(home, token="123:test-token"):
    cls = getattr(pairing, "VerifiedOwnerStore", None)
    assert cls is not None, "owner proof must not be inferred from chat admission"
    return cls(token, home=home)


def enroll(s, uid="12345678"):
    challenge = s.begin()
    code = s.prove(challenge, uid)
    assert code
    assert not s.is_owner(uid), "a Telegram message alone must not grant ownership"
    assert s.confirm(code) == uid
    return challenge, code


def test_local_confirmation_persists_verified_sender_without_changing_settings(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("proactive:\n  enabled: false\nplatforms:\n  telegram:\n    allow_from: ['*']\n")
    before = config.read_bytes()
    s = store(home)
    enroll(s)
    assert store(home).is_owner("12345678")  # New process/object sees the grant.
    assert not store(home).is_owner("87654321")
    assert not pairing.PairingStore().is_approved("telegram", "12345678")
    assert config.read_bytes() == before
    state = json.loads(s.path.read_text())
    assert state["owner"] == "12345678"
    assert not state.get("pending")


import concurrent.futures
import shutil
import pytest


@pytest.mark.parametrize("uid", ["", "0", "-1", "@owner", "123 45", "１２３", "12345678\n", "9223372036854775808", None, True])
def test_only_literal_positive_transport_user_ids_can_prove(tmp_path, uid):
    s = store(tmp_path)
    challenge = s.begin()
    before = s.path.read_bytes()
    assert s.prove(challenge, uid) is None
    assert s.path.read_bytes() == before


def test_repeated_sender_cannot_fill_request_or_replace_their_code(tmp_path):
    s = store(tmp_path)
    challenge = s.begin()
    code = s.prove(challenge, "12345678")
    before = s.path.read_bytes()
    assert s.prove(challenge, "12345678") is None
    assert s.path.read_bytes() == before
    assert s.confirm(code) == "12345678"


def test_multiple_claimants_need_local_selection_not_first_come(tmp_path):
    s = store(tmp_path)
    challenge = s.begin()
    thief = s.prove(challenge, "87654321")
    owner = s.prove(challenge, "12345678")
    assert not s.is_owner("87654321")
    assert not s.is_owner("12345678")
    assert s.confirm(owner) == "12345678"
    assert s.confirm(thief) is None
    assert not s.is_owner("87654321")


def test_forgery_expiry_replay_revocation_and_scope_are_fail_closed(tmp_path, monkeypatch):
    s = store(tmp_path / "one")
    challenge = s.begin()
    assert s.prove("forged", "12345678") is None
    code = s.prove(challenge, "12345678")
    assert s.confirm("forged") is None
    monkeypatch.setattr(pairing.time, "time", lambda: json.loads(s.path.read_text())["pending"]["expires"])
    assert s.prove(challenge, "87654321") is None
    assert s.confirm(code) is None
    monkeypatch.undo()
    challenge, code = enroll(s)
    assert s.confirm(code) is None
    assert s.prove(challenge, "87654321") is None
    with pytest.raises(ValueError):
        s.begin()  # No silent rebind.
    assert not store(s.home, "999:other-bot").is_owner("12345678")
    other = store(tmp_path / "two")
    other.path.parent.mkdir(parents=True)
    shutil.copyfile(s.path, other.path)
    assert not other.is_owner("12345678")
    s.revoke()
    assert not store(s.home).is_owner("12345678")
    assert s.confirm(code) is None
    assert s.prove(challenge, "12345678") is None
    enroll(s, "87654321")
    assert store(s.home).is_owner("87654321")


def test_plaintext_codes_and_bot_credentials_are_not_persisted_or_logged(tmp_path, caplog):
    s = store(tmp_path)
    challenge = s.begin()
    code = s.prove(challenge, "12345678")
    content = s.path.read_text() + caplog.text
    for secret in (challenge, code, "123:test-token"):
        assert secret not in content


def test_concurrent_local_confirmations_cannot_overwrite_winner(tmp_path):
    s = store(tmp_path)
    challenge = s.begin()
    codes = [s.prove(challenge, uid) for uid in ("12345678", "87654321")]
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda c: store(tmp_path).confirm(c), codes))
    assert sum(r is not None for r in results) == 1
    assert store(tmp_path).is_owner(next(r for r in results if r))


def test_persistence_failure_never_reports_owner_saved(tmp_path, monkeypatch):
    s = store(tmp_path)
    challenge = s.begin()
    code = s.prove(challenge, "12345678")
    monkeypatch.setattr(pairing, "_secure_write", lambda *a: None)
    with pytest.raises(OSError, match="could not be confirmed"):
        s.confirm(code)
    assert not store(tmp_path).is_owner("12345678")


def test_corrupt_store_fails_closed_and_can_only_be_reset_locally(tmp_path):
    s = store(tmp_path)
    s.path.parent.mkdir(parents=True)
    s.path.write_text("not json")
    assert not s.is_owner("12345678")
    with pytest.raises(ValueError):
        s.begin()
    s.revoke()
    enroll(s)


@pytest.mark.linux_only
def test_owner_file_permissions_and_atomic_failure_preserve_previous_record(tmp_path, monkeypatch):
    import stat
    s = store(tmp_path)
    challenge = s.begin()
    code = s.prove(challenge, "12345678")
    before = s.path.read_bytes()
    assert stat.S_IMODE(s.path.stat().st_mode) == 0o600
    def fail(*args):
        raise OSError("synthetic atomic replace failure")
    monkeypatch.setattr(pairing, "atomic_replace", fail)
    with pytest.raises(OSError):
        s.confirm(code)
    assert s.path.read_bytes() == before
    assert not s.is_owner("12345678")


def test_candidate_limit_does_not_grant_first_claimant_and_local_reset_cancels_all(tmp_path):
    s = store(tmp_path)
    challenge = s.begin()
    codes = [s.prove(challenge, str(uid)) for uid in range(100, 108)]
    assert all(codes)
    assert s.prove(challenge, "999") is None
    assert not any(s.is_owner(str(uid)) for uid in range(100, 108))
    replacement = s.begin()
    assert s.confirm(codes[0]) is None
    assert s.prove(challenge, "999") is None
    code = s.prove(replacement, "999")
    assert s.confirm(code) == "999"


def test_cross_process_confirmations_have_a_single_durable_winner(tmp_path):
    import os
    import subprocess
    import sys
    s = store(tmp_path)
    challenge = s.begin()
    codes = [s.prove(challenge, uid) for uid in ("12345678", "87654321")]
    program = (
        "import json,sys; from pathlib import Path; from gateway.pairing import VerifiedOwnerStore; "
        "v=json.load(sys.stdin); print(VerifiedOwnerStore('123:test-token',home=Path(v['home'])).confirm(v['code']) or '')"
    )
    env = {"HOME": str(tmp_path), "CLOVER_HOME": str(tmp_path), "PATH": os.defpath}
    for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        if name in os.environ: env[name] = os.environ[name]
    def confirm(code):
        result = subprocess.run([sys.executable, "-c", program],
                                input=json.dumps({"home": str(tmp_path), "code": code}),
                                text=True, capture_output=True, env=env, timeout=20)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(confirm, codes))
    winners = [r for r in results if r]
    assert len(winners) == 1
    assert store(tmp_path).is_owner(winners[0])
