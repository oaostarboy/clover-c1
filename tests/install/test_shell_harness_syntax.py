"""Executable checks for the install/update sandbox harness.

Nothing here reads shell source.  The real ``scripts/dev-sandbox.sh`` (stage 1)
and ``scripts/sandbox/stage2-run.sh`` (stage 2) are run end to end with the
host-privileged boundary tools (``bwrap``, ``unshare``, ``slirp4netns``,
``curl``) replaced by small observers on ``PATH``.  We then assert on what the
harness actually handed to that boundary: the environment given to bubblewrap
and the URL the installer shortcut asked ``curl`` to fetch.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")

_OBSERVER = """#!/usr/bin/env bash
# Record argv one entry per line (NUL-safe enough for these fixtures), then stop.
out="${{OBSERVE_DIR:?}}/{name}.argv"
: > "$out"
for arg in "$@"; do printf '%s\\x1e' "$arg" >> "$out"; done
{extra}
"""

_UNSHARE = """#!/usr/bin/env bash
# Stand-in for `unshare --user --net ... CMD`: drop the namespace flags and run CMD.
while [ "$#" -gt 0 ]; do
  case "$1" in --*) shift ;; *) break ;; esac
done
exec "$@"
"""

_SLIRP = """#!/usr/bin/env bash
# Stage 2 blocks until the network helper reports ready on fd 3.
echo ready >&3
sleep 0.2
"""


def _write_tool(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _observer(directory: Path, name: str, extra: str = "") -> None:
    _write_tool(directory, name, _OBSERVER.format(name=name, extra=extra))


def _argv(observe_dir: Path, name: str) -> list[str]:
    raw = (observe_dir / f"{name}.argv").read_text(encoding="utf-8")
    return [part for part in raw.split("\x1e") if part != ""] if raw else []


def _run_sandbox(
    tmp_path: Path, extra_args: list[str], node_dir: str, monkeypatch
) -> tuple[Path, Path]:
    """Run the real stage-1 script; return (observation dir, sandbox root)."""
    assert BASH is not None
    tools = tmp_path / "tools"
    observe = tmp_path / "observe"
    tools.mkdir()
    observe.mkdir()
    _observer(tools, "bwrap")
    _observer(tools, "curl")
    _write_tool(tools, "unshare", _UNSHARE)
    _write_tool(tools, "slirp4netns", _SLIRP)

    repo = tmp_path / "repo"
    repo.mkdir()
    git_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    (repo / "scripts").mkdir()
    (repo / "scripts/install.sh").write_text("#!/usr/bin/env bash\necho installed\n")
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "fixture"],
    ):
        subprocess.run(cmd, cwd=repo, env=git_env, stdin=subprocess.DEVNULL, check=True)

    sandbox_dir = ".sandbox-fixture"
    env = {
        **git_env,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "OBSERVE_DIR": str(observe),
        "CLOVER_SANDBOX_SOURCE_ROOT": str(repo),
        "CLOVER_DEV_SANDBOX_DIR": sandbox_dir,
        "DEV_SANDBOX_NODE_DIR": node_dir,
        "HOME": str(tmp_path / "home"),
    }
    (tmp_path / "home").mkdir()
    result = subprocess.run(
        # The `install` shortcut must be the first word; options follow it.
        [BASH, str(ROOT / "scripts/dev-sandbox.sh"), *extra_args[:1], "--root", "--persistent", *extra_args[1:]],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return observe, repo / sandbox_dir


def _setenv(argv: list[str]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for index, arg in enumerate(argv):
        if arg == "--setenv" and index + 2 < len(argv):
            pairs[argv[index + 1]] = argv[index + 2]
    return pairs


def _load_proxy(sandbox_root: Path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["proxy.py", str(sandbox_root / "root/http"), str(sandbox_root / "root/certs"), "unused"],
    )
    spec = importlib.util.spec_from_file_location(
        "sandbox_proxy_for_harness_test", ROOT / "scripts/sandbox/proxy.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(BASH is None, reason="bash is required for shell syntax checks")
@pytest.mark.parametrize(
    "script",
    ("scripts/dev-sandbox.sh", "scripts/sandbox/stage2-run.sh", "tests/install/install-update-e2e.sh"),
)
def test_install_update_helpers_have_valid_bash_syntax(script: str) -> None:
    assert BASH is not None
    result = subprocess.run(
        [BASH, "-n", str(ROOT / script)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.linux_only
@pytest.mark.skipif(BASH is None, reason="bash is required")
def test_installer_shortcut_fetches_a_url_the_fixture_proxy_serves(tmp_path, monkeypatch):
    observe, sandbox_root = _run_sandbox(tmp_path, ["install"], "", monkeypatch)

    # Run the payload bwrap was asked to execute (the real installer shortcut)
    # with an observing `curl`, and require the URL it requests to resolve to a
    # fixture file through the proxy's own lookup.
    argv = _argv(observe, "bwrap")
    payload = argv[argv.index("sandbox-command") + 1 :]
    assert payload[:2] == ["bash", "-c"], payload[:3]
    shortcut = payload[2]
    tools = tmp_path / "tools"
    payload_file = tmp_path / "installer-shortcut.sh"
    payload_file.write_text(shortcut, encoding="utf-8")
    subprocess.run(
        [BASH, str(payload_file)],
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "OBSERVE_DIR": str(observe)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    curl_args = _argv(observe, "curl")
    urls = [arg for arg in curl_args if "://" in arg]
    assert len(urls) == 1, f"installer shortcut must pass exactly one URL to curl: {curl_args}"
    url = urlsplit(urls[0])
    assert url.scheme == "https" and url.hostname, urls[0]

    proxy = _load_proxy(sandbox_root, monkeypatch)
    served = proxy.file_for(url.hostname, url.path)
    assert served is not None, f"proxy has no fixture for {urls[0]}"
    # The fixture the proxy serves IS this checkout's installer.
    assert served.read_bytes() == b"#!/usr/bin/env bash\necho installed\n"


@pytest.mark.linux_only
@pytest.mark.skipif(BASH is None, reason="bash is required")
def test_node_tls_trusts_the_sandbox_proxy_ca_not_the_real_bundle(tmp_path, monkeypatch):
    observe, _ = _run_sandbox(tmp_path, ["true"], "", monkeypatch)
    env = _setenv(_argv(observe, "bwrap"))

    # The proxy re-signs every host with the sandbox CA, so Node must trust
    # exactly the file the other TLS clients (curl, git) are given.
    assert env["NODE_EXTRA_CA_CERTS"] == env["SSL_CERT_FILE"] == env["CURL_CA_BUNDLE"]
    assert env["NODE_EXTRA_CA_CERTS"] != "/work/certs/real-ca.pem"


@pytest.mark.linux_only
@pytest.mark.skipif(BASH is None, reason="bash is required")
@pytest.mark.parametrize(
    "shadowed",
    ["/usr/local", "/usr/local/node-26", "/opt/hostedtoolcache/node/26/x64", "/home/runner/.nvm/v26"],
)
def test_node_gyp_is_not_pointed_at_host_headers_hidden_by_the_sandbox(
    tmp_path, monkeypatch, shadowed
):
    # /usr/local is an empty bind inside the sandbox; headers there do not exist.
    observe, _ = _run_sandbox(tmp_path, ["true"], shadowed, monkeypatch)

    assert "npm_config_nodedir" not in _setenv(_argv(observe, "bwrap"))


@pytest.mark.linux_only
@pytest.mark.skipif(BASH is None, reason="bash is required")
@pytest.mark.parametrize("visible", ["/nix/store/abc-nodejs", "/usr/lib/node"])
def test_node_gyp_keeps_nodedir_for_a_prefix_visible_in_the_sandbox(tmp_path, monkeypatch, visible):
    observe, _ = _run_sandbox(tmp_path, ["true"], visible, monkeypatch)

    assert _setenv(_argv(observe, "bwrap"))["npm_config_nodedir"] == visible
