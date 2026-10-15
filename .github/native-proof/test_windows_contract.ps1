# Portable PowerShell parser/argument/exit contracts, not native Windows proof.
param([Parameter(Mandatory=$true)][string]$Python)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$tokens = $null; $errors = $null
$path = Join-Path $PSScriptRoot 'windows.ps1'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$function = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-Proof' }, $true)
if ($function.Count -ne 1) { throw 'Expected exactly one native invocation helper' }
# Execute the exact helper definition, not the host-guarded installer body.
Invoke-Expression $function[0].Extent.Text
$script:py = $Python
$proof = Join-Path $PSScriptRoot 'proof.py'
Invoke-Proof identity --help | Out-Null
$rejected = $false
try { Invoke-Proof unknown-subcommand 2>$null } catch { $rejected = $true }
if (-not $rejected) { throw 'Nonzero native exit was swallowed' }
Write-Output 'PASS: PowerShell parses; exact invocation helper forwards args and rejects a real nonzero command'
