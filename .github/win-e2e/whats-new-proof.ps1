# Helpers for the "version name + what's new after /update" Windows e2e
# (dot-sourced after supervisor-proof.ps1).

function New-ReleaseCandidate {
  # A real commit on top of the branch head that looks like the next release:
  # version 1.1.1, and the 1.1.1 section of RELEASE_NOTES.md filled in. Built in
  # a throwaway worktree of the install's own object store, so the update can
  # pull it exactly like a published release. Returns the commit sha.
  $d = $env:INSTALL_DIR
  git -C $d fetch -q origin "+refs/heads/$($env:HEAD_BRANCH):refs/remotes/origin/$($env:HEAD_BRANCH)"
  if ($LASTEXITCODE -ne 0) { throw "fetch of $env:HEAD_BRANCH failed" }
  $wt = "$env:RUNNER_TEMP\candidate"
  Remove-Item -Recurse -Force $wt -ErrorAction SilentlyContinue
  git -C $d worktree add -q --detach $wt "refs/remotes/origin/$($env:HEAD_BRANCH)"
  if ($LASTEXITCODE -ne 0) { throw "worktree add failed" }
  $edit = @'
import re, sys, pathlib
root = pathlib.Path(sys.argv[1])
init = root / "clover_cli" / "__init__.py"
t = init.read_text(encoding="utf-8")
t = re.sub(r'__version__ = "[^"]+"', '__version__ = "1.1.1"', t)
init.write_text(t, encoding="utf-8", newline="\n")
py = root / "pyproject.toml"
t = py.read_text(encoding="utf-8")
t = re.sub(r'(?m)^version = "[^"]+"', 'version = "1.1.1"', t, count=1)
py.write_text(t, encoding="utf-8", newline="\n")
lock = root / "uv.lock"
t = lock.read_text(encoding="utf-8")
t = re.sub(r'(name = "clover-c1"\r?\nversion = )"[^"]+"', r'\1"1.1.1"', t, count=1)
lock.write_text(t, encoding="utf-8", newline="\n")
notes = root / "RELEASE_NOTES.md"
t = notes.read_text(encoding="utf-8")
bullets = "- Proof bullet one: the update message names the release.\n- Proof bullet two: it lists what is new.\n- Proof bullet three: nothing extra is sent.\n"
t = re.sub(r"(## 1\.1\.1 \| Clover C1\.1\.1 \| )[^\n]*\n<!-- draft -->\n- TODO[^\n]*\n", lambda m: m.group(1) + "2026-10-07\n" + bullets, t, count=1)
if "Proof bullet one" not in t:
    raise SystemExit("could not fill the 1.1.1 notes section")
notes.write_text(t, encoding="utf-8", newline="\n")
'@
  $editFile = "$env:RUNNER_TEMP\make_candidate.py"
  Set-Content -Path $editFile -Value $edit -Encoding utf8
  python $editFile $wt
  if ($LASTEXITCODE -ne 0) { throw "candidate edit failed" }
  git -C $wt add -A
  git -C $wt -c user.name=ci -c user.email=ci@example.invalid commit -q -m "ci: release candidate 1.1.1 (proof only)"
  if ($LASTEXITCODE -ne 0) { throw "candidate commit failed" }
  $sha = (git -C $wt rev-parse HEAD).Trim()
  git -C $d worktree remove --force $wt
  Write-Host "release candidate commit: $sha"
  return $sha
}

function Set-MainIsCommit([string]$targetSha) {
  # Same trick as Set-MainIsTarget, for a commit that exists only in the
  # install's object store: `clover update` / the gateway's /update fetch
  # origin/main as usual, and git reads the candidate in its place.
  $d = $env:INSTALL_DIR
  git -C $d fetch -q origin "+refs/heads/main:refs/remotes/origin/main"
  if ($LASTEXITCODE -ne 0) { throw "fetch failed" }
  $mainSha = (git -C $d rev-parse refs/remotes/origin/main).Trim()
  Remove-MainFromHistory $targetSha $mainSha
  git -C $d replace -f $mainSha $targetSha
  if ($LASTEXITCODE -ne 0) { throw "git replace failed" }
  git -C $d checkout -q -B main HEAD
  git -C $d branch -q -u origin/main
  Write-Host "main $mainSha now reads as candidate $targetSha; local main at $(git -C $d rev-parse --short HEAD)"
}

function Pin-MainToCommit([string]$sha) {
  # Real origin/main can move while a run is in flight (it did, mid-proof). Point
  # whatever main is NOW back at the candidate so "nothing new" really is nothing.
  Set-MainIsCommit $sha
}

function Get-IrcLineCount {
  $p = "$env:RUNNER_TEMP\irc.log"
  if (Test-Path $p) { return @(Get-Content $p).Count } else { return 0 }
}

function Send-ChatUpdate {
  # /update from the chat user, over the local IRC server, into the running gateway.
  New-Item -ItemType File -Force -Path "$env:RUNNER_TEMP\irc.trigger" | Out-Null
}

function Show-UpdateNotificationLog([int]$logStart) {
  # Which gateway sent the completion message: print gateway start lines and the
  # notification lines in order, so the log shows the message came after the
  # restart, from the new process.
  Get-LogLines | Select-Object -Skip $logStart | Select-String -Pattern 'Starting Clover Gateway|Update finished|post-update notification|Update concluded after updater death|Update notification deferred|Update watcher' | ForEach-Object { Write-Host "[notify-log] $($_.Line)" }
}
