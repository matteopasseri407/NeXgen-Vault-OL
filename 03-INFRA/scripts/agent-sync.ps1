$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "agent_sync.py"
$py = (Get-Command py -ErrorAction SilentlyContinue).Source
if ($py) { & $py -3 $script @args } else { & python $script @args }
exit $LASTEXITCODE
