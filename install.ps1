#requires -Version 5.1
<#
  install.ps1 - Windows-native twin of install.sh (bootstrap / preflight).

  Deterministic first-run helper. It does NOT replace the AI-guided installer
  (INIT.md): it does the mechanical part -- check prerequisites, verify the
  vault scaffold, detect your CLIs, compute your install profile -- and then
  hands you the exact next step. Safe to re-run; it writes nothing in -Check
  mode, and only creates missing scaffold directories otherwise.

  Usage:
    .\install.ps1            preflight + guided profile + next steps
    .\install.ps1 -Check     preflight only (no questions, no writes)
#>
param([switch]$Check)

$ErrorActionPreference = "Continue"
$ROOT = $PSScriptRoot
$script:MissReq = 0

# ---- console style (mirrors agent-doctor.ps1's [OK]/[WARN]/[FAIL] tags) ----
function ok($m)   { Write-Host "  [OK]   $m" -ForegroundColor Green }
function bad($m)  { Write-Host "  [FAIL] $m" -ForegroundColor Red }
function warn($m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function sec($m)  { Write-Host "`n$m" -ForegroundColor White }
function dim($m)  { Write-Host $m -ForegroundColor DarkGray }

function have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

function Show-Banner {
  Write-Host ""
  Write-Host "=== NeXgen Engine - bootstrap ===" -ForegroundColor Cyan
  dim "  One Git-backed vault for AI-tool config and memory."
}

# Resolve the Python 3 interpreter: 'python3' first (WSL/Chocolatey installs,
# py-launcher shims), else 'python' -- the python.org Windows installer only
# ships 'python', and that 'python' is always Python 3 (Python 2 is long EOL).
function Get-PyBin {
  if (have "python3") { return "python3" }
  if (have "python") {
    & python -c "import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { return "python" }
  }
  return ""
}

function Test-Prereqs {
  sec "1 - Prerequisites"
  if (have "git") {
    $gitVer = (git --version 2>$null)
    ok "git - $gitVer"
  } else {
    bad "git MISSING (required) -> winget install Git.Git"
    $script:MissReq = 1
  }

  $py = Get-PyBin
  if ($py) {
    $pyVer = (& $py --version 2>&1)
    ok "python3 - $pyVer (as '$py')"
  } else {
    bad "python3 MISSING (required) -> winget install Python.Python.3"
    $script:MissReq = 1
  }

  $hasYaml = $false
  if ($py) {
    & $py -c "import yaml" 2>$null | Out-Null
    $hasYaml = ($LASTEXITCODE -eq 0)
  }
  if ($hasYaml) {
    ok "PyYAML (python module 'yaml')"
  } else {
    bad "PyYAML MISSING (required for the engine) -> pip install pyyaml"
    $script:MissReq = 1
  }

  if (have "npx") { ok "node/npx - needed to mount MCP servers & external skills" }
  else { warn "node/npx not found - needed only if you mount MCP servers or npx skills" }

  if (have "jq") { ok "jq" }
  else { warn "jq not found - the MULTI sync scripts use Python on Windows, jq is not required here" }

  if (have "curl.exe") { ok "curl" }
  else { warn "curl not found - needed only for the MULTI health scripts" }

  if (have "gpg") { ok "gpg - for the encrypted 99-SECRETS archive" }
  else { warn "gpg not found - needed only if you store secrets in 99-SECRETS/ (winget install GnuPG.Gpg4win)" }
}

function Test-Scaffold {
  sec "2 - Vault scaffold"
  foreach ($d in @("01-NOTES", "02-PROJECTS", "04-NOW", "99-INDEX", "99-SECRETS")) {
    $path = Join-Path $ROOT $d
    if (Test-Path -LiteralPath $path -PathType Container) {
      ok "$d/"
    } elseif ($Check) {
      bad "$d/ missing (read-only check)"
      $script:MissReq = 1
    } else {
      warn "$d/ missing - creating"
      New-Item -ItemType Directory -Force -Path $path | Out-Null
      New-Item -ItemType File -Force -Path (Join-Path $path ".gitkeep") | Out-Null
    }
  }
  foreach ($f in @("INIT.md", "00-START-HERE.md", "99-INDEX\USER-PROFILE.md", "03-INFRA\agent-universal-layer\instructions\AGENTS.md")) {
    $path = Join-Path $ROOT $f
    if (Test-Path -LiteralPath $path -PathType Leaf) {
      ok "$f"
    } else {
      bad "$f MISSING -- is this a full clone?"
      $script:MissReq = 1
    }
  }
}

function Get-DetectedClis {
  sec "3 - Agentic CLIs detected on this machine"
  $found = @()
  if (have "claude") { ok "Claude Code (claude)"; $found += "claude" }
  if (have "codex") { ok "Codex (codex)"; $found += "codex" }
  if (have "opencode") { ok "OpenCode (opencode)"; $found += "opencode" }
  $homeDir = [Environment]::GetFolderPath("UserProfile")
  if (Test-Path -LiteralPath (Join-Path $homeDir ".gemini")) {
    ok "Antigravity/Gemini (~/.gemini)"
    $found += "antigravity"
  }
  if ($found.Count -eq 0) {
    warn "No agentic CLI found on PATH."
    dim "     The installer needs a filesystem-capable agent (Claude Code, Codex, OpenCode,"
    dim "     Antigravity). A plain web chat (claude.ai / gemini) CANNOT write files."
  }
  return $found
}

function Invoke-GuidedProfile {
  sec "4 - Guided profile (no files written -- recommendation only)"
  if ([Console]::IsInputRedirected) {
    warn "not an interactive terminal -- skipping questions"
    return
  }
  $ncli = Read-Host "  How many CLIs will you run? [1 / 2+]"
  $nmach = Read-Host "  How many machines to keep aligned? [1 / 2+]"
  $arch = Read-Host "  Architecture? [L=Local-only / C=Cloud-server (VPS)]"

  $installProfile = "MINIMAL"
  if ("$ncli$nmach" -match "2") { $installProfile = "MULTI" }
  Write-Host ""
  Write-Host "  Profile: $installProfile" -ForegroundColor Cyan
  if ($installProfile -eq "MINIMAL") {
    dim "     -> one CLI, one machine. No agent-sync/doctor/timer. Mount MCP + skills by hand."
  } else {
    dim "     -> multiple CLIs/machines. Use agent-sync (apply/guard) to keep them aligned."
  }
  if ($arch -match "^[Cc]") {
    dim "     -> Cloud-Server: deploy 03-INFRA/deploy/ on a VPS and set the tunnel env vars."
  } else {
    dim "     -> Local-Only: native web search, model vision for OCR, no remote automations."
  }
}

function Show-NextSteps {
  sec "5 - Next step"
  if ($script:MissReq -eq 1) {
    bad "Fix the MISSING required items above, then re-run: .\install.ps1"
    return 1
  }
  Write-Host ""
  Write-Host "  Preflight passed." -ForegroundColor Green -NoNewline
  Write-Host " Finish the install with the AI-guided installer:"
  Write-Host ""
  Write-Host "    1. Open INIT.md."
  Write-Host "    2. Paste its whole content into a filesystem-capable agent CLI"
  dim "       (Claude Code, Codex, OpenCode, Antigravity) opened in this folder --"
  dim "       NOT a plain web chat, which cannot write files."
  Write-Host "    3. Answer its interview; it writes 99-INDEX/USER-PROFILE.md and mounts"
  Write-Host "       your MCP servers and skills."
  Write-Host ""
  dim "  MCP dialects reference: 03-INFRA/agent-universal-layer/mcp/render.py"
  dim "  Secrets workflow: 99-SECRETS/README.md"
  return 0
}

# ---- run ---------------------------------------------------------------
Show-Banner
Test-Prereqs
Test-Scaffold
Get-DetectedClis | Out-Null
if (-not $Check) { Invoke-GuidedProfile }
$exitCode = Show-NextSteps
exit $exitCode
