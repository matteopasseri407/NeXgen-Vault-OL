[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)] [string[]]$PytestArgs)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$python = $null
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
  $python = @('py', '-3')
} else {
  $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
  if ($pythonCommand) { $python = @($pythonCommand.Source) }
}
if (-not $python) {
  Write-Error 'tests/run.ps1: Python non trovato. Installa Python 3 e riprova.'
  exit 1
}
$pythonArgs = if ($python.Count -gt 1) { $python[1..($python.Count - 1)] } else { @() }

& $python[0] @pythonArgs -c 'import pytest, yaml' 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Error 'tests/run.ps1: servono pytest e PyYAML, installa con: py -3 -m pip install --user pytest pyyaml'
  exit 1
}

$leakScan = Join-Path $PSScriptRoot '..\leak-scan\leak_scan.py'
$patterns = Join-Path $PSScriptRoot '..\leak-scan\leak_patterns.yaml'
if ((Test-Path -LiteralPath $leakScan) -and (Test-Path -LiteralPath $patterns)) {
  & $python[0] @pythonArgs $leakScan --patterns $patterns --tree (Join-Path $PSScriptRoot 'fixtures')
  if ($LASTEXITCODE -ne 0) { Write-Error 'tests/run.ps1: leak nelle fixture'; exit 1 }
}

& $python[0] @pythonArgs -m pytest -v @PytestArgs
exit $LASTEXITCODE
