"""Execute the installer helpers, without dot-sourcing/running the installer.

Portable runs exercise real npm through a process-group adapter, NOT cmd.exe.
Windows runs use the actual cmd.exe/taskkill helper. No network is required.
Run: python -m unittest discover -s tests -p test_install_ps1_locked_node_deps.py -v
PWSH and NPM may point at private test tools; nothing is installed globally.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PWSH = os.environ.get("PWSH") or shutil.which("pwsh") or ""
NPM = os.environ.get("NPM") or shutil.which("npm") or ""

# Only this launcher boundary is adapted on POSIX. PowerShell's real helper
# chooses the command, enforces lock policy, handles status, and retains logs.
ADAPTER = r'''
import ctypes, os, shlex, signal, subprocess, sys, time
exe, args, cwd, log, ceiling = sys.argv[1:]
if sys.platform.startswith('linux'):
    # Reap killed grandchildren as well as npm; do not leave test zombies to
    # a container's PID 1. This is a portable test adapter, not Windows proof.
    if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError('Could not enable child subreaper')
with open(log, 'w') as stream:
    p = subprocess.Popen([exe, *shlex.split(args)], cwd=cwd, stdout=stream,
                         stderr=subprocess.STDOUT, start_new_session=True)
    try:
        code = p.wait(timeout=int(ceiling))
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    time.sleep(0.02)
            except ChildProcessError:
                break
        code = 124
print(code)
'''


def psquote(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(PWSH and NPM, "private PowerShell and npm tools required")
class LockedNodeInstall(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="clover locked npm ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "space and 中文"
        self.base.mkdir()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.adapter = self.base / "native adapter.py"
        self.adapter.write_text(ADAPTER)
        self.harness = self.base / "helpers.ps1"
        # AST extraction works for both the old nested helper and the new
        # shared helper. Reject parser errors before evaluating any source.
        self.prelude = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(SOURCE, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
foreach ($name in @('_Invoke-NativeWithTimeout', '_Run-NpmInstall')) {
    $fn = $ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true)
    if (-not $fn) { throw "Missing helper $name" }
    Invoke-Expression $fn.Extent.Text
}
function Write-Info($s) { Write-Host $s }
function Write-Warn($s) { Write-Host $s }
function Write-Success($s) { Write-Host $s }
function Show-NpmCertHint($s) { return $false }
function Write-NpmDebugLogTail($NpmOutput) {}
'''.replace("SOURCE", psquote(ROOT / "scripts/install.ps1"))
        self.env = {**os.environ, "npm_config_cache": str(self.base / "cache"),
                    "npm_config_userconfig": str(self.base / "no-user-npmrc"),
                    "npm_config_globalconfig": str(self.base / "no-global-npmrc")}

    def tearDown(self):
        if target := os.environ.get("NODE_INSTALL_TEST_EVIDENCE"):
            dest = Path(target) / self._testMethodName
            dest.mkdir(parents=True, exist_ok=True)
            for log in self.base.glob("*.log"):
                shutil.copy2(log, dest / log.name)
            for logs in self.base.glob("*.log.npm"):
                shutil.copytree(logs, dest / logs.name, dirs_exist_ok=True)
            (dest / "hashes.json").write_text(json.dumps({
                "before": getattr(self, "before", None), "after": self.hashes()}, indent=2))

    def run_ps(self, body, portable=False):
        import sys
        adapter = ""
        if portable and os.name != "nt":
            adapter = r'''
function _Invoke-NativeWithTimeout($exePath, $argLine, $workDir, $logPath, $timeoutSec) {
    $code = & PYTHON ADAPTER $exePath $argLine $workDir $logPath $timeoutSec
    if ($LASTEXITCODE -ne 0) { throw 'Portable launcher failed' }
    return [int]$code
}
'''.replace("PYTHON", psquote(sys.executable)).replace("ADAPTER", psquote(self.adapter))
        # Windows PowerShell 5.1 needs a BOM for the deliberate Unicode paths.
        self.harness.write_text(self.prelude + adapter + body, encoding="utf-8-sig")
        p = subprocess.run([PWSH, "-NoProfile", "-File", str(self.harness)],
                           text=True, capture_output=True, env=self.env, timeout=35)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        lines = [line.removeprefix("RESULT:") for line in p.stdout.splitlines()
                 if line.startswith("RESULT:")]
        self.assertTrue(lines, p.stdout + p.stderr)
        return json.loads(lines[-1]), p.stdout

    def manifest(self, relative, value):
        target = self.repo / relative / "package.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value))

    def fixture(self, script=None):
        self.manifest("", {"name": "locked-fixture", "version": "1.0.0", "private": True,
            "workspaces": ["ui-tui", "apps/*", "ui-tui/packages/*"],
            "dependencies": {"root-dep": "file:vendor/root-dep"},
            "scripts": {"postinstall": script or "node -e \"require('fs').writeFileSync('root-ran', 'yes')\""}})
        self.manifest("vendor/root-dep", {"name": "root-dep", "version": "1.0.0"})
        self.manifest("ui-tui", {"name": "fixture-tui", "version": "1.0.0",
            "dependencies": {"fixture-ink": "file:packages/ink", "fixture-shared": "file:../apps/shared"}})
        self.manifest("ui-tui/packages/ink", {"name": "fixture-ink", "version": "1.0.0"})
        self.manifest("apps/shared", {"name": "fixture-shared", "version": "1.0.0"})
        self.manifest("apps/desktop", {"name": "fixture-desktop", "version": "1.0.0",
            "scripts": {"postinstall": "node -e \"require('fs').writeFileSync('desktop-ran', 'yes')\""}})
        p = subprocess.run([NPM, "install", "--package-lock-only", "--ignore-scripts",
            "--offline", "--no-audit", "--no-fund"], cwd=self.repo,
            text=True, capture_output=True, env=self.env, timeout=25)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "*.json"], check=True, capture_output=True)
        self.before = self.hashes()

    def hashes(self):
        return {str(p.relative_to(self.repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.repo.rglob("*.json") if "node_modules" not in p.parts}

    def install(self, scope="root", timeout=10):
        log = self.base / (scope + " output.log")
        status_before = subprocess.check_output(["git", "-C", str(self.repo), "diff", "--name-only"], text=True)
        body = f'''
$nodeDepsTimeoutSec = {timeout}
$previousLocation = (Get-Location).Path
$ok = _Run-NpmInstall 'Fixture' {psquote(self.repo)} {psquote(log)} {psquote(NPM)} {psquote(scope)} {timeout}
Write-Host ('RESULT:' + (@{{ok=$ok; locationRestored=((Get-Location).Path -eq $previousLocation); eap=[string]$ErrorActionPreference}} | ConvertTo-Json -Compress))
'''
        result, stdout = self.run_ps(body, portable=True)
        self.assertTrue(result["locationRestored"])
        self.assertEqual(result["eap"], "Stop")
        self.assertEqual(self.hashes(), self.before, "tracked manifests/lock must remain byte-identical")
        status = subprocess.check_output(["git", "-C", str(self.repo), "diff", "--name-only"], text=True)
        self.assertEqual(status, status_before)
        return result, log, stdout

    def test_root_command_does_not_expand_workspaces(self):
        self.fixture()
        result, log, _ = self.install()
        self.assertTrue(result["ok"])
        self.assertTrue((self.repo / "node_modules/root-dep").exists())
        self.assertFalse((self.repo / "node_modules/fixture-desktop").exists())
        self.assertFalse((self.repo / "apps/desktop/desktop-ran").exists())
        self.assertTrue((self.repo / "root-ran").exists(), "lifecycle scripts must run")
        self.assertTrue(log.exists(), "preserve successful command logs too")

    def test_tui_keeps_root_and_transitive_workspace_dependencies(self):
        self.fixture()
        result, _, _ = self.install("tui")
        self.assertTrue(result["ok"])
        for name in ["root-dep", "fixture-tui", "fixture-ink", "fixture-shared"]:
            self.assertTrue((self.repo / "node_modules" / name).exists(), name)
        self.assertFalse((self.repo / "node_modules/fixture-desktop").exists())

    def test_desktop_keeps_full_workspace_lifecycle_semantics(self):
        self.fixture()
        result, _, _ = self.install("desktop")
        self.assertTrue(result["ok"])
        self.assertTrue((self.repo / "node_modules/fixture-tui").exists())
        self.assertTrue((self.repo / "apps/desktop/desktop-ran").exists())

    def test_real_lifecycle_failure_preserves_lock_and_debug_logs(self):
        self.fixture('node -e "process.exit(23)"')
        result, log, stdout = self.install()
        self.assertFalse(result["ok"])
        self.assertIn("exit code 23", stdout)
        self.assertTrue(log.exists())
        logs = Path(str(log) + ".npm")
        self.assertTrue(list(logs.glob("*debug*.log")), "capture npm phase/version diagnostics")

    def test_missing_lock_refuses_without_install_fallback(self):
        self.fixture()
        (self.repo / "package-lock.json").unlink()
        self.before = self.hashes()
        result, _, stdout = self.install()
        self.assertFalse(result["ok"])
        self.assertFalse((self.repo / "node_modules").exists())
        self.assertIn("lockfile", stdout.lower())

    def test_inconsistent_lock_refuses_without_repair(self):
        self.fixture()
        manifest = json.loads((self.repo / "package.json").read_text())
        manifest["dependencies"]["unlocked"] = "99.99.99"
        self.manifest("", manifest)
        # Missing registry dependency is deliberately offline: npm ci must
        # refuse, never regenerate the lock or proceed to lifecycle execution.
        self.env["npm_config_offline"] = "true"
        self.before = self.hashes()
        result, _, _ = self.install()
        self.assertFalse(result["ok"])
        self.assertFalse((self.repo / "root-ran").exists())

    def test_timeout_preserves_lock_logs_and_reaps_lifecycle_child(self):
        self.fixture("node hang.cjs")
        (self.repo / "hang.cjs").write_text("const fs=require('fs'),cp=require('child_process'); "
            "const c=cp.spawn(process.execPath,['-e','setTimeout(()=>{},120000)'],{stdio:'ignore'}); "
            "fs.writeFileSync('child.pid',String(c.pid)); setTimeout(()=>{},120000);")
        result, log, stdout = self.install(timeout=3)
        self.assertFalse(result["ok"])
        self.assertIn("timed out", stdout)
        self.assertTrue(log.exists())
        self.assertTrue((self.repo / "child.pid").exists(), "must reach genuine lifecycle hang")
        pid = int((self.repo / "child.pid").read_text())
        if os.name == "nt":
            output = subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}"], text=True)
            self.assertNotIn(str(pid), output)
        else:
            stat = Path(f"/proc/{pid}/stat")
            self.assertFalse(stat.exists(), "portable timeout must reap the child, not leave a zombie")

    def test_exact_scope_argv_and_timeout_reach_native_boundary(self):
        self.fixture()
        result, _ = self.run_ps(f'''
$script:calls = @()
function _Invoke-NativeWithTimeout($exePath,$argLine,$workDir,$logPath,$timeoutSec) {{
    $script:calls += @{{exe=$exePath; argv=$argLine; cwd=$workDir; timeout=$timeoutSec}}
    return 0
}}
foreach ($scope in @('root','tui','desktop')) {{
    [void](_Run-NpmInstall 'Fixture' {psquote(self.repo)} {psquote(self.base / 'out.log')} {psquote(NPM)} $scope 7)
}}
Write-Host ('RESULT:' + (ConvertTo-Json -InputObject $script:calls -Compress))
''')
        selections = ["--workspaces=false", "--workspace=ui-tui --include-workspace-root", "--workspaces --include-workspace-root"]
        self.assertEqual(len(result), 3)
        for call, selection in zip(result, selections):
            self.assertEqual(call["argv"], f'ci {selection} --no-audit --no-fund --foreground-scripts --loglevel=verbose --logs-dir="{self.base / "out.log.npm"}"')
            self.assertEqual(call["cwd"], str(self.repo))
            self.assertEqual(call["timeout"], 7)

    def test_node_stage_routes_root_and_tui_without_disabling_browser(self):
        self.fixture()
        result, _ = self.run_ps(f'''
$fn = $ast.Find({{param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Install-NodeDeps'}}, $true)
Invoke-Expression $fn.Extent.Text
$HasNode = $true; $InstallDir = {psquote(self.repo)}; $env:TEMP = {psquote(self.base)}
$env:NODE_DEPS_TIMEOUT = '7'; $script:calls = @(); $script:browser = $false
function Ensure-NodeExeOnPath {{}}
function Install-BrowserUseCli {{}}
function Install-CuaDriver {{}}
function _Run-NpmInstall($label,$dir,$log,$npm,$scope,$timeout) {{
    $script:calls += @{{label=$label; cwd=$dir; scope=$scope; timeout=$timeout}}
    return $true
}}
function _Invoke-NativeWithTimeout($exe,$argv,$dir,$log,$timeout) {{
    if ($argv -eq '--yes playwright install chromium') {{ $script:browser = $true }}
    return 0
}}
Install-NodeDeps
Write-Host ('RESULT:' + (@{{calls=$script:calls; browser=$script:browser}} | ConvertTo-Json -Compress))
''')
        self.assertTrue(result["browser"])
        self.assertEqual([c["scope"] for c in result["calls"]], ["root", "tui"])
        for call in result["calls"]:
            self.assertEqual(call["cwd"], str(self.repo))
            self.assertEqual(call["timeout"], 7)

    def test_desktop_stage_uses_bounded_locked_helper_without_install_fallback(self):
        self.fixture()
        failing_npm = self.base / 'failing-npm.ps1'
        failing_npm.write_text('$global:LASTEXITCODE = 23')
        result, _ = self.run_ps(f'''
$fn = $ast.Find({{param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Install-Desktop'}}, $true)
Invoke-Expression $fn.Extent.Text
$InstallDir = {psquote(self.repo)}; $env:TEMP = {psquote(self.base)}
$env:NODE_DEPS_TIMEOUT = '7'; $script:calls = @(); $threw = $false
function Test-Node {{}}
function Get-Command {{ return [pscustomobject]@{{Source={psquote(failing_npm)}}} }}
function Test-ElectronPkgStagedMissingDist {{ return $false }}
function _Run-NpmInstall($label,$dir,$log,$npm,$scope,$timeout) {{
    $script:calls += @{{cwd=$dir; scope=$scope; timeout=$timeout}}
    return $false
}}
try {{ Install-Desktop }} catch {{ $threw = $true }}
Write-Host ('RESULT:' + (@{{calls=$script:calls; threw=$threw}} | ConvertTo-Json -Compress))
''')
        self.assertTrue(result["threw"])
        self.assertEqual(result["calls"], [{"cwd": str(self.repo), "scope": "desktop", "timeout": 7}])

    def test_native_helper_cleans_tree_if_polling_throws(self):
        result, _ = self.run_ps(r'''
$script:killed = @(); $threw = $false
function Start-Process { return [pscustomobject]@{HasExited=$false; ExitCode=$null; Id=12345} }
function Start-Sleep { throw 'poll interrupted' }
function taskkill { $script:killed = @($args) }
try { _Invoke-NativeWithTimeout 'npm.cmd' 'ci' 'repo' 'missing.log' 10 } catch { $threw = $true }
Write-Host ('RESULT:' + (@{threw=$threw; killed=$script:killed} | ConvertTo-Json -Compress))
''')
        self.assertTrue(result["threw"])
        self.assertEqual(result["killed"], ["/T", "/F", "/PID", 12345])

    def test_native_proof_preserves_npm_output_and_debug_directory(self):
        logs = self.base / 'temp logs'
        logs.mkdir()
        (logs / 'clover-npm-browser-123.log').write_text('npm command phase')
        debug = logs / 'clover-npm-browser-123.log.npm'
        debug.mkdir()
        (debug / 'test-debug-0.log').write_text('npm verbose node v22.23.3')
        (logs / 'unrelated-secret.txt').write_text('must not collect')
        proof = ROOT / '.github/native-proof/windows.ps1'
        result, _ = self.run_ps(f'''
$proofAst = [System.Management.Automation.Language.Parser]::ParseFile({psquote(proof)}, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) {{ throw ($parseErrors | Out-String) }}
$fn = $proofAst.Find({{param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Save-NpmEvidence'}}, $true)
if (-not $fn) {{ throw 'Save-NpmEvidence is not defined' }}
Invoke-Expression $fn.Extent.Text
$env:TEMP = {psquote(logs)}
Save-NpmEvidence {psquote(self.base / 'evidence')}
Write-Host 'RESULT:true'
''')
        self.assertTrue(result)
        saved = self.base / 'evidence/npm'
        self.assertEqual((saved / debug.name / 'test-debug-0.log').read_text(), 'npm verbose node v22.23.3')
        self.assertEqual((saved / 'clover-npm-browser-123.log').read_text(), 'npm command phase')
        self.assertFalse((saved / 'unrelated-secret.txt').exists())

    def test_native_helper_builds_quoted_cmd_and_returns_real_exit_code(self):
        result, _ = self.run_ps(r'''
$script:captured = $null
function Start-Process { param($FilePath,$ArgumentList,$WorkingDirectory,[switch]$NoNewWindow,[switch]$PassThru)
    $script:captured = @{exe=$FilePath; args=$ArgumentList; cwd=$WorkingDirectory}
    return [pscustomobject]@{HasExited=$true; ExitCode=37; Id=12345}
}
$env:ComSpec = 'C:\Windows\System32\cmd.exe'
$code = _Invoke-NativeWithTimeout 'C:\node space 中文\npm.cmd' 'ci --workspaces=false' 'C:\repo space 中文' 'C:\log space 中文\out.log' 3
Write-Host ('RESULT:' + (@{code=$code; captured=$script:captured} | ConvertTo-Json -Compress))
''')
        self.assertEqual(result["code"], 37)
        self.assertIn('"C:\\node space 中文\\npm.cmd" ci --workspaces=false', result["captured"]["args"])
        self.assertIn('> "C:\\log space 中文\\out.log" 2>&1', result["captured"]["args"])
        self.assertEqual(result["captured"]["cwd"], 'C:\\repo space 中文')

    def test_native_helper_timeout_requests_whole_tree_cleanup(self):
        result, _ = self.run_ps(r'''
$script:killed = @()
function Start-Process { return [pscustomobject]@{HasExited=$false; ExitCode=$null; Id=12345} }
function taskkill { $script:killed = @($args) }
$code = _Invoke-NativeWithTimeout 'npm.cmd' 'ci' 'repo' 'missing.log' -1
Write-Host ('RESULT:' + (@{code=$code; killed=$script:killed} | ConvertTo-Json -Compress))
''')
        self.assertEqual(result["code"], 124)
        self.assertEqual(result["killed"], ["/T", "/F", "/PID", 12345])


if __name__ == "__main__":
    unittest.main()
