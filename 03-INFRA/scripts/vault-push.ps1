# vault-push.ps1 — Windows twin of vault-push.sh.
#
# Thin wrapper only: the actual logic lives in agent_sync.py's `vault-push`
# subcommand (single cross-platform implementation, shared with
# vault-push.sh on Linux/Mac -- see agent_sync.py's own module docstring
# and docs/sync-contract.md). This script's only job is resolving
# agent_sync.py (same resolution chain as vault-push.sh: AGENT_ENGINE_ROOT,
# then the persisted engine-root indirection, then this script's own
# co-located sibling) and forwarding every argument into it.
#
# DEGRADED EMERGENCY LANE: if agent_sync.py or a Python 3 interpreter cannot
# be resolved AND KNOWLEDGE_VAULT_REMOTE is explicitly set (the same
# emergency/bootstrap opt-in agent_sync.py itself treats as a complete
# override), this script falls back to a minimal PowerShell+git commit/
# push/rebase-retry lane instead of hard-failing. No mirrors in this lane
# (see Invoke-DegradedVaultPush below).
$ErrorActionPreference = "Stop"
# PowerShell 7.3+ opt-in, defaults to $true there: a non-zero exit from a
# native command (agent_sync.py's own vault-push exit codes 1/2/75 in the
# delegated path below, git in the degraded lane) would otherwise become a
# TERMINATING error under $ErrorActionPreference = "Stop" -- including
# entirely benign non-zero exits this script checks $LASTEXITCODE for by
# design (e.g. `git diff --cached --quiet` returning 1 just means "there
# are staged changes"). Harmless no-op on Windows PowerShell 5.1, where this
# preference variable does not exist as a special case.
$PSNativeCommandUseErrorActionPreference = $false

$HomeDir = [Environment]::GetFolderPath("UserProfile")

# Python 3 resolution: python3 first, else python (verified to actually be
# Python 3) -- same order and same reasoning as install.ps1's Get-PyBin
# (the python.org Windows installer only ships 'python', and that 'python'
# is always Python 3; python3 covers WSL/Chocolatey installs and py-launcher
# shims). Deliberately NOT the 'py -3' launcher agent-sync.ps1/council.ps1
# use: this script needs an explicit presence check it can branch on before
# falling into the degraded lane, not just an invocation that may 404.
function Get-PyBin {
  if (Get-Command python3 -ErrorAction SilentlyContinue) { return "python3" }
  if (Get-Command python -ErrorAction SilentlyContinue) {
    & python -c "import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { return "python" }
  }
  return $null
}

# Resolution chain for agent_sync.py, same order agent_sync.py's own
# Env.engine_root resolution uses on the POSIX side:
#   1. AGENT_ENGINE_ROOT (explicit override, wins unconditionally)
#   2. the persisted engine-root indirection: %USERPROFILE%\.local\bin\
#      agent-sync.ps1's OWN link target's directory (the Windows-named twin
#      of the symlink agent_sync.py's Env._persisted_engine_root reads on
#      POSIX) -- found even with no env var exported, the normal way anyone
#      types this command, instead of silently reverting to the vault
#      default. Best-effort: utils() falls back to a plain copy (no
#      LinkType) when this process lacks symlink privilege, in which case
#      there is no target to follow and this step is skipped.
#   3. this script's own co-located sibling (the common case, same pattern
#      as agent-sync.ps1/council.ps1).
$AgentSync = $null
if ($env:AGENT_ENGINE_ROOT) {
  $candidate = Join-Path $env:AGENT_ENGINE_ROOT "scripts\agent_sync.py"
  if (Test-Path -LiteralPath $candidate -PathType Leaf) { $AgentSync = $candidate }
}
if (-not $AgentSync) {
  $linkedTwin = Join-Path $HomeDir ".local\bin\agent-sync.ps1"
  $item = Get-Item -LiteralPath $linkedTwin -ErrorAction SilentlyContinue
  if ($item -and $item.LinkType -and $item.Target) {
    $target = @($item.Target)[0]
    $candidate = Join-Path (Split-Path -Parent $target) "agent_sync.py"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $AgentSync = $candidate }
  }
}
if (-not $AgentSync) {
  $candidate = Join-Path $PSScriptRoot "agent_sync.py"
  if (Test-Path -LiteralPath $candidate -PathType Leaf) { $AgentSync = $candidate }
}

$PyBin = Get-PyBin

if ($AgentSync -and $PyBin) {
  & $PyBin $AgentSync "vault-push" @args
  exit $LASTEXITCODE
}

$UnavailableReason = if (-not $AgentSync) { "agent_sync.py not found -- engine checkout is incomplete" } else { "python3/python not found" }

if (-not $env:KNOWLEDGE_VAULT_REMOTE) {
  # [Console]::Error.WriteLine, not Write-Error: under
  # $ErrorActionPreference = "Stop", Write-Error is a TERMINATING error, so
  # the `exit 2` right after it would never run and the process would exit
  # with PowerShell's own default error code instead of the "2" this
  # contract promises (same pattern vault-groom.ps1 already uses).
  [Console]::Error.WriteLine("vault-push: $UnavailableReason")
  exit 2
}

[Console]::Error.WriteLine("vault-push: degraded emergency lane (engine unavailable: $UnavailableReason)")

# ---- degraded emergency lane (PowerShell + git only, no python) -----------
# Mirrors agent_sync.py's own vault-push commit/publish shape exactly
# (_vault_push_locked / _vault_push_publish) but stripped to what plain
# PowerShell+git can do: no mirrors (a mirror realignment failure must never
# look like the authoritative push failed, and this lane has no structured
# config loader to resolve mirror names from), no strict remote-name
# validation (this IS the emergency override, same trust level
# KNOWLEDGE_VAULT_REMOTE already carries everywhere else in the engine).

function Get-DegradedLock {
  param([string]$LockPath, [double]$TimeoutSeconds)
  $dir = Split-Path -Parent $LockPath
  if ($dir -and -not (Test-Path -LiteralPath $dir)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
  }
  $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
  while ($true) {
    try {
      return [System.IO.File]::Open($LockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    } catch {
      if ([DateTime]::UtcNow -ge $deadline) { return $null }
      Start-Sleep -Milliseconds 100
    }
  }
}

function Invoke-DegradedVaultPush {
  param([string[]]$Arguments)

  # -m MSG [file ...]: same shape as agent_sync.py's own
  # _parse_vault_push_args (-m MSG, glued -mMSG, "--" stops flag parsing and
  # takes every remaining argument as a file verbatim).
  $msg = $null
  $files = @()
  $i = 0
  while ($i -lt $Arguments.Count) {
    $a = $Arguments[$i]
    if ($a -eq "-m") {
      if ($i + 1 -ge $Arguments.Count) {
        [Console]::Error.WriteLine("vault-push: argument missing for -m")
        return 2
      }
      $msg = $Arguments[$i + 1]
      $i += 2
      continue
    }
    if ($a.StartsWith("-m") -and $a -ne "-m") {
      $msg = $a.Substring(2)
      $i += 1
      continue
    }
    if ($a -eq "--") {
      if ($i + 1 -lt $Arguments.Count) { $files += $Arguments[($i + 1)..($Arguments.Count - 1)] }
      break
    }
    $files += $a
    $i += 1
  }
  if (-not $msg) {
    [Console]::Error.WriteLine('vault-push: needs -m "message"')
    return 2
  }

  $vaultDir = if ($env:AGENT_VAULT_DATA) { $env:AGENT_VAULT_DATA } elseif ($env:KNOWLEDGE_VAULT_PATH) { $env:KNOWLEDGE_VAULT_PATH } else { Join-Path $HomeDir "KnowledgeVault" }
  if (-not (Test-Path -LiteralPath $vaultDir -PathType Container)) {
    Write-Host "vault-push: vault not found ($vaultDir)"
    return 1
  }

  # Best-effort lock on the SAME lock file agent_sync.py's SyncRunLock would
  # use, so a concurrent guard/apply cycle on this machine still serializes
  # against a degraded-lane push. Best-effort only: a FileStream open with
  # FileShare.None cannot fail for any reason OTHER than "someone else has
  # it open" on a healthy filesystem, so a timeout here reliably means real
  # contention, not a false negative -- but this lane already exists because
  # the Python engine is not usable, so it degrades to unlocked rather than
  # blocking the emergency lane on a lock primitive that itself misbehaves.
  $lockFile = if ($env:AGENT_SYNC_LOCK_FILE) { $env:AGENT_SYNC_LOCK_FILE } else { Join-Path $HomeDir ".local\state\agent-sync.lock" }
  $lockTimeout = if ($env:AGENT_SYNC_LOCK_TIMEOUT_SECONDS) { [double]$env:AGENT_SYNC_LOCK_TIMEOUT_SECONDS } else { 2.0 }
  $lockHandle = Get-DegradedLock -LockPath $lockFile -TimeoutSeconds $lockTimeout
  if (-not $lockHandle) {
    [Console]::Error.WriteLine("vault-push: sync lock busy (another agent-sync/vault-push is running) -- aborting")
    return 75
  }
  try {
    if ($files.Count -gt 0) {
      & git -C $vaultDir add -- @files
      if ($LASTEXITCODE -ne 0) {
        Write-Host "vault-push: git add failed"
        return 1
      }
    }
    & git -C $vaultDir diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
      Write-Host "vault-push: nothing staged, nothing to commit"
      return 0
    }
    & git -C $vaultDir commit -q -m $msg
    if ($LASTEXITCODE -ne 0) {
      Write-Host "vault-push: commit failed"
      return 1
    }
    $short = (& git -C $vaultDir rev-parse --short HEAD | Out-String).Trim()
    Write-Host "vault-push: commit $short"

    if ($env:KNOWLEDGE_VAULT_REMOTE -eq "local" -or $env:KNOWLEDGE_VAULT_REMOTE -eq "none") {
      Write-Host "vault-push: push skipped (Local-Only mode, remote=$($env:KNOWLEDGE_VAULT_REMOTE))"
      return 0
    }

    $branch = if ($env:KNOWLEDGE_VAULT_BRANCH) { $env:KNOWLEDGE_VAULT_BRANCH } else { "main" }
    $remote = $env:KNOWLEDGE_VAULT_REMOTE
    & git -C $vaultDir push $remote $branch
    if ($LASTEXITCODE -eq 0) {
      Write-Host "vault-push: push $remote OK"
      return 0
    }
    & git -C $vaultDir fetch --prune $remote $branch
    if ($LASTEXITCODE -ne 0) {
      Write-Host "vault-push: $remote OFFLINE -- the commit stays local; run agent-sync publish later"
      return 1
    }
    $status = (& git -C $vaultDir status --porcelain --untracked-files=no)
    if ($status) {
      Write-Host "vault-push: $remote rejected but the working tree has uncommitted changes -- NOT rebasing, resolve by hand"
      return 1
    }
    & git -C $vaultDir rebase "$remote/$branch"
    if ($LASTEXITCODE -eq 0) {
      & git -C $vaultDir push $remote $branch
      if ($LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine("vault-push: $remote still rejected after rebase -- try again")
        return 1
      }
      Write-Host "vault-push: push $remote OK (after a clean rebase)"
      return 0
    }
    & git -C $vaultDir rebase --abort
    [Console]::Error.WriteLine("vault-push: $remote DIVERGENCE WITH CONFLICT -- needs a manual 'git pull --rebase $remote $branch'")
    return 1
  } finally {
    $lockHandle.Close()
  }
}

exit (Invoke-DegradedVaultPush -Arguments $args)
