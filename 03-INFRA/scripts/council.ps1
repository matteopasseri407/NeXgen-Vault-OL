$ErrorActionPreference = "Stop"
$parent = Split-Path $PSScriptRoot -Parent
$script = Join-Path $parent "agent-universal-layer\council\council.py"
$py = (Get-Command py -ErrorAction SilentlyContinue).Source
if ($py) { & $py -3 $script @args } else { & python $script @args }
exit $LASTEXITCODE
