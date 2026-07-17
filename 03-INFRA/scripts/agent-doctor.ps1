#requires -Version 5.1
<#
  agent-doctor.ps1 - Windows alignment check (port of agent-doctor.sh).
  Read-only. Exit 0 if no FAIL, 1 otherwise.
  Usage: .\agent-doctor.ps1            readable report
         .\agent-doctor.ps1 -Summary   one-line summary (for healthcheck)
  NOTE: on Windows, Codex/Antigravity may be copies or symlinks of the canonical
  file. Claude only uses a lightweight pointer to the canonical file to avoid
  duplication with OpenCode when both files are loaded in the same context.
#>
param([switch]$Summary, [switch]$Strict)

$ErrorActionPreference = "Continue"
$HomeDir = [Environment]::GetFolderPath("UserProfile")
$Vault   = if ($env:KNOWLEDGE_VAULT_PATH) { $env:KNOWLEDGE_VAULT_PATH } else { Join-Path $HomeDir "KnowledgeVault" }
$Branch  = if ($env:KNOWLEDGE_VAULT_BRANCH) { $env:KNOWLEDGE_VAULT_BRANCH } else { "main" }
$Layer   = Join-Path $Vault "03-INFRA\agent-universal-layer"
$EngineInfra = Split-Path -Parent $PSScriptRoot
$RenderPy = Join-Path $EngineInfra "agent-universal-layer\mcp\render.py"
$Canon   = Join-Path $Layer "instructions\AGENTS.md"
$AppDataRoot = if ($env:APPDATA) { $env:APPDATA } else { Join-Path $HomeDir "AppData\Roaming" }
$AppDataOcJson = Join-Path $AppDataRoot "opencode\opencode.json"
$LegacyOcJson  = Join-Path $HomeDir ".config\opencode\opencode.json"
$OcJson = if ((Test-Path -LiteralPath $LegacyOcJson) -and -not (Test-Path -LiteralPath $AppDataOcJson)) {
  $LegacyOcJson
} else {
  $AppDataOcJson
}

function Resolve-NexgenPython {
  $candidates = @()
  foreach ($name in @("python3", "python")) {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if ($found) { $candidates += [pscustomobject]@{ Command = $found.Source; Prefix = @() } }
  }
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) { $candidates += [pscustomobject]@{ Command = $py.Source; Prefix = @("-3") } }
  foreach ($candidate in $candidates) {
    $prefix = @($candidate.Prefix)
    $candidateCommand = $candidate.Command
    & $candidateCommand @prefix -c "import sys, yaml; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { return $candidate }
  }
  return $null
}

$NexgenPython = Resolve-NexgenPython
$NexgenPythonCommand = if ($NexgenPython) { $NexgenPython.Command } else { $null }
$NexgenPythonPrefix = if ($NexgenPython) { @($NexgenPython.Prefix) } else { @() }

$RemoteConfigError = $false
if ($env:KNOWLEDGE_VAULT_REMOTE) {
  $Remote = $env:KNOWLEDGE_VAULT_REMOTE
  $Mirrors = if ($env:KNOWLEDGE_VAULT_MIRRORS) { @($env:KNOWLEDGE_VAULT_MIRRORS -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) } else { @() }
} else {
  $AgentSyncPy = Join-Path $PSScriptRoot "agent_sync.py"
  if ($NexgenPython) {
    $Remote = (& $NexgenPythonCommand @NexgenPythonPrefix $AgentSyncPy config authoritative_remote 2>$null)
    $RemoteExit = $LASTEXITCODE
    $Mirrors = @(& $NexgenPythonCommand @NexgenPythonPrefix $AgentSyncPy config mirrors 2>$null | Where-Object { $_ })
    $MirrorsExit = $LASTEXITCODE
  } else {
    $RemoteConfigError = $true
    $Remote = "origin"
    $Mirrors = @()
    $RemoteExit = 1
    $MirrorsExit = 1
  }
  if (-not $Remote -or $RemoteExit -ne 0 -or $MirrorsExit -ne 0) {
    $RemoteConfigError = $true
    $Remote = "origin"
    $Mirrors = @()
  }
}

$script:PASS = 0; $script:WARN = 0; $script:FAILN = 0; $script:FAILS = @()
function ok($m)   { $script:PASS++;  if (-not $Summary) { Write-Host "  [OK]   $m" -ForegroundColor Green } }
function warn($m) { $script:WARN++;  if (-not $Summary) { Write-Host "  [WARN] $m" -ForegroundColor Yellow } }
function bad($m)  { $script:FAILN++; $script:FAILS += $m; if (-not $Summary) { Write-Host "  [FAIL] $m" -ForegroundColor Red } }
function sec($m)  { if (-not $Summary) { Write-Host "`n$m" -ForegroundColor White } }
function gitc([string[]]$GitArgs) { (& git -C $Vault @GitArgs 2>$null) }
function httpcode($url, $headers, [string]$method = "Get") {
  try { (Invoke-WebRequest -Uri $url -Method $method -TimeoutSec 6 -Headers $headers -UseBasicParsing -ErrorAction Stop).StatusCode }
  catch { if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 } }
}
function hashOf($p) { if (Test-Path -LiteralPath $p) { (Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash } else { "" } }

if (-not $Summary) { Write-Host "=== agent-doctor: agent alignment check ===" -ForegroundColor White }

sec "Host"
ok "detected: windows ($env:COMPUTERNAME)"
try {
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $userPathLength = if ($userPath) { $userPath.Length } else { 0 }
  $combinedPath = (@($machinePath, $userPath) | Where-Object { $_ }) -join ";"
  $combinedPathLength = $combinedPath.Length
  $processPathLength = if ($env:Path) { $env:Path.Length } else { 0 }
  if (($userPathLength -gt 8191) -or ($combinedPathLength -gt 8191) -or ($processPathLength -gt 8191)) {
    bad "PATH exceeds cmd.exe's 8191-character inherited-variable limit (user=$userPathLength, combined=$combinedPathLength, current-process=$processPathLength); shorten it and start a fresh shell before trusting Node/npm launchers"
  } else {
    ok "PATH length is safe for cmd.exe (user=$userPathLength, combined=$combinedPathLength, current-process=$processPathLength)"
  }
} catch {
  warn "could not read the Windows User PATH length"
}

sec "Vault (memory) - authoritative remote and mirrors"
if ($RemoteConfigError) { bad "invalid sync remote config - run: agent-sync config authoritative_remote" } else { ok "sync remote config resolved ($Remote)" }
if (Test-Path -LiteralPath (Join-Path $Vault ".git")) {
  if ($Remote -eq "local" -or $Remote -eq "none") {
    # Local-Only sentinel (matches agent_sync.py's pull()/publish(): env.remote
    # in ("local", "none")): there is no authoritative remote to compare
    # against, so a fetch/ahead/behind/mirror check would otherwise try
    # `git fetch` from a remote literally named "local"/"none" -- which never
    # exists -- and hard-FAIL a correctly configured Local-Only install.
    ok "Local-Only mode ($Remote): no authoritative remote to compare against, skipping fetch/ahead/behind/mirror checks"
    $d = @(gitc @("status","--porcelain","--untracked-files=no")).Where({ $_ }).Count
    if ($d -eq 0) { ok "working tree clean (tracked files)" } else { warn "$d tracked files not committed" }
  } else {
    gitc @("fetch","--prune",$Remote,$Branch) | Out-Null
    $b = (gitc @("rev-list","--count","$Branch..$Remote/$Branch")); if (-not $b) { $b = "?" }
    $a = (gitc @("rev-list","--count","$Remote/$Branch..$Branch")); if (-not $a) { $a = "?" }
    $d = @(gitc @("status","--porcelain","--untracked-files=no")).Where({ $_ }).Count
    if ("$b" -eq "0") { ok "aligned with $Remote/$Branch (0 behind)" } else { bad "$b commits behind the cloud" }
    if ("$a" -eq "0" -or "$a" -eq "?") {
      ok "no unpublished local commits"
    } else {
      $oldest_ts = (gitc @("log","--reverse","--format=%ct","$Remote/$Branch..$Branch") | Select-Object -First 1)
      if ($oldest_ts) {
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $age = $now - [int]$oldest_ts
        if ($age -gt 7200) {
          $hours = [math]::Floor($age / 3600)
          bad "$a dangling commit(s) in Vault (oldest is ${hours}h old) - RUN vault-push!"
        } else {
          warn "$a unpublished local commits (recent, < 2h old)"
        }
      } else {
        warn "$a unpublished local commits"
      }
    }
    if ($d -eq 0)     { ok "working tree clean (tracked files)" } else { warn "$d tracked files not committed (blocks the pull)" }
    foreach ($mirror in $Mirrors) {
      gitc @("fetch","--prune",$mirror,$Branch) | Out-Null
      $authHead = (gitc @("rev-parse","$Remote/$Branch"))
      $mirrorHead = (gitc @("rev-parse","$mirror/$Branch"))
      if ($authHead -and "$authHead" -eq "$mirrorHead") { ok "mirror $mirror aligned with authoritative $Remote" }
      else { warn "mirror $mirror differs from authoritative $Remote (canonical Vault remains $Remote)" }
    }
  }
} else { bad "the vault is not a git repo: $Vault" }

sec "Canonical instructions (single AGENTS.md, Claude anti-duplication pointer)"
if (Test-Path -LiteralPath $Canon) { ok "canonical file present"; $ch = hashOf $Canon } else { bad "missing canonical file $Canon"; $ch = "" }
$ClaudeFile = Join-Path $HomeDir "CLAUDE.md"
if (Test-Path -LiteralPath $ClaudeFile) {
  $ci = Get-Item -LiteralPath $ClaudeFile -Force
  $ct = Get-Content -Raw -LiteralPath $ClaudeFile -ErrorAction SilentlyContinue
  if (-not $ci.LinkType -and $ct.Contains($Canon) -and $ct.Contains("compatibility pointer")) { ok "Claude pointer -> canonical AGENTS.md" }
  else { bad "Claude.md must be a lightweight pointer, not a copy/symlink of the canonical file ($ClaudeFile)" }
} else { bad "missing Claude pointer ($ClaudeFile)" }
# NOTE (found on Fedora, NOT YET VERIFIED on Windows): on Fedora, Antigravity
# actually reads ~/.gemini/config/AGENTS.md, not ~/ANTIGRAVITY.md (that symlink
# existed but the app never read it, a false "ok" for days). Expected Windows
# path by analogy: %USERPROFILE%\.gemini\config\AGENTS.md -- to be confirmed
# with a real behavioral probe (agy -p) the first time this runs on a real
# Windows machine, don't trust the hash comparison alone.
foreach ($pair in @(@("Codex", (Join-Path $HomeDir ".codex\AGENTS.md")), @("Antigravity", (Join-Path $HomeDir ".gemini\config\AGENTS.md")))) {
  $f = $pair[1]
  if ((hashOf $f) -and (hashOf $f) -eq $ch) { ok "$($pair[0]) = canonical AGENTS.md (identical content)" }
  else { bad "$($pair[0]) NOT aligned with the canonical file ($f)" }
}
if (Test-Path -LiteralPath $OcJson) {
  if (Select-String -Quiet -LiteralPath $OcJson -Pattern "instructions/AGENTS.md") { ok "OpenCode instructions -> AGENTS.md" } else { bad "OpenCode instructions do NOT point to AGENTS.md" }
} else { bad "missing $OcJson" }

sec "Canonical bootstrap hygiene (size budget, pointer integrity)"
# Additive, read-only guardrails on the single AGENTS.md bootstrap and its
# load-on-demand detail notes. WARN-only by design: they surface drift (a
# bloated bootstrap, a pointer to a note renamed/removed out from under the
# list) without ever flipping a green doctor red on a pre-existing condition.
# Budgets are overridable via env for installs with different conventions.
$BootstrapMaxBytes = if ($env:NEXGEN_BOOTSTRAP_MAX_BYTES) { [int]$env:NEXGEN_BOOTSTRAP_MAX_BYTES } else { 40000 }
$NoteMaxBytes = if ($env:NEXGEN_NOTE_MAX_BYTES) { [int]$env:NEXGEN_NOTE_MAX_BYTES } else { 16000 }
if (Test-Path -LiteralPath $Canon) {
  $canonBytes = (Get-Item -LiteralPath $Canon).Length
  if ($canonBytes -gt $BootstrapMaxBytes) {
    warn "bootstrap AGENTS.md is $canonBytes bytes, over the $BootstrapMaxBytes-byte budget - move task-specific content into a load-on-demand note (override: NEXGEN_BOOTSTRAP_MAX_BYTES)"
  } else {
    ok "bootstrap AGENTS.md within budget ($canonBytes/$BootstrapMaxBytes bytes)"
  }
  $notesDir = Join-Path $Vault "03-INFRA"
  $oversized = @()
  if (Test-Path -LiteralPath $notesDir) {
    foreach ($note in @(Get-ChildItem -LiteralPath $notesDir -Filter '*.md' -File -ErrorAction SilentlyContinue)) {
      if ($note.Length -gt $NoteMaxBytes) { $oversized += "$($note.Name) ($($note.Length)b)" }
    }
  }
  if ($oversized.Count -eq 0) { ok "detail notes within the $NoteMaxBytes-byte budget" }
  else { warn "oversized detail note(s) over $NoteMaxBytes bytes, consider splitting: $($oversized -join ', ') (override: NEXGEN_NOTE_MAX_BYTES)" }
  # Load-on-demand pointer integrity: every vault-relative note path in
  # backticks must resolve under the vault. The literal placeholder
  # 03-INFRA/<topic>.md in the editing-discipline prose is skipped (angle
  # brackets); ~-rooted paths and URLs never match the vault-prefix set.
  $canonText = Get-Content -Raw -LiteralPath $Canon
  $ptrMatches = [regex]::Matches($canonText, '`((?:03-INFRA|99-INDEX|04-NOW|02-PROJECTS|01-NOTES|00-START-HERE)[^`]*)`')
  $refs = @($ptrMatches | ForEach-Object { $_.Groups[1].Value } | Where-Object { $_ -match '\.md$' -and $_ -notmatch '[<>]' } | Select-Object -Unique)
  $missingPtr = @($refs | Where-Object { -not (Test-Path -LiteralPath (Join-Path $Vault $_)) })
  if ($refs.Count -eq 0) { ok "no vault-relative bootstrap pointers to verify" }
  elseif ($missingPtr.Count -eq 0) { ok "all $($refs.Count) bootstrap load-on-demand pointers resolve" }
  else { warn "bootstrap load-on-demand pointer(s) not found under the vault: $($missingPtr -join ', ') - a renamed/removed note leaves a dead pointer" }
  # Required invariant rules present in the canonical AGENTS.md (guards the
  # non-negotiable security/behaviour rules from silently vanishing - the
  # vault<->public drift class). Read-only, WARN-only; skips if the checker or
  # its rules file isn't present in this engine tree.
  $rulesCheck = Join-Path $PSScriptRoot "check_required_rules.py"
  $rulesFile = Join-Path $EngineInfra "agent-universal-layer\instructions\required-rules.txt"
  if ($NexgenPython -and (Test-Path -LiteralPath $rulesCheck) -and (Test-Path -LiteralPath $rulesFile)) {
    $rulesOut = & $NexgenPythonCommand @NexgenPythonPrefix $rulesCheck $Canon $rulesFile 2>$null
    if ($LASTEXITCODE -eq 0) {
      ok "canonical AGENTS.md carries all required invariant rules"
    } else {
      $miss = (@($rulesOut | Where-Object { $_ -match '^\s+- ' }) | ForEach-Object { $_ -replace '^\s+- ', '' }) -join '; '
      warn "canonical AGENTS.md is missing required invariant rule(s): $(if ($miss) { $miss } else { 'see check' })"
    }
  }
} else {
  warn "canonical AGENTS.md not found, skipping bootstrap hygiene checks"
}

sec "Deterministic agent utilities"
$agentNow = Get-Command agent-now -ErrorAction SilentlyContinue
if (-not $agentNow) {
  $agentNow = Get-Command agent-now.ps1 -ErrorAction SilentlyContinue
}
if ($agentNow) {
  try {
    $payload = (& $agentNow.Source 2>$null) | Out-String
    if ($payload -match '"source"\s*:\s*"system_clock"' -and $payload -match '"local_time"') {
      ok "agent-now available and working"
    }
    else {
      bad "agent-now present but output invalid"
    }
  }
  catch {
    bad "agent-now present but not runnable"
  }
}
else {
  bad "agent-now not in PATH (run agent-sync.ps1)"
}

sec "Windows PATH persistence (user registry)"
# agent-sync apply appends %USERPROFILE%\.local\bin to the user's registry
# PATH (HKCU:\Environment) so agent-now/agent-doctor/etc resolve as bare
# commands in every NEW terminal, not just the one that happened to run the
# installer.
$localBin = Join-Path $HomeDir ".local\bin"

# PRIMARY probe (external architect's review, accepted 2026-07-13): actually
# resolve 'agent-sync' as a bare command in a FRESH process (a brand-new
# powershell.exe, not this already-running session's own command table), so
# execution-policy restrictions and PATHEXT/.cmd-association problems
# surface here instead of only showing up the first time a user opens a
# real new terminal -- a directory merely being a substring of some PATH
# value proves nothing about whether the command actually runs. -NoProfile:
# a broken/slow profile script must not make this probe itself unreliable.
$freshProbeOk = $false
try {
  $probeOutput = (& powershell.exe -NoProfile -Command "if (Get-Command agent-sync -ErrorAction SilentlyContinue) { 'FOUND' } else { 'MISSING' }" 2>&1 | Out-String)
  $freshProbeOk = ($LASTEXITCODE -eq 0) -and ($probeOutput -match 'FOUND')
} catch {
  $freshProbeOk = $false
}

if ($freshProbeOk) {
  ok "agent-sync resolves as a bare command in a fresh process"
} else {
  # Fallback/diagnostic detail only, NOT an alternate pass path: the
  # persisted registry value (HKCU:\Environment) and this CURRENT process's
  # own $env:Path snapshot, read directly via Get-ItemProperty (not just
  # $env:Path, which is only a snapshot taken at THIS process's start and
  # can't prove what a brand-new terminal will actually inherit) -- just to
  # say WHY the fresh probe likely failed.
  $procPathHasIt = @($env:Path -split ';') -contains $localBin
  $regPathHasIt = $false
  try {
    $regPathValue = (Get-ItemProperty -Path "HKCU:\Environment" -Name "Path" -ErrorAction Stop).Path
    if ($regPathValue) { $regPathHasIt = @($regPathValue -split ';') -contains $localBin }
  } catch {
    $regPathHasIt = $false
  }
  if ($regPathHasIt -or $procPathHasIt) {
    bad "$localBin is on the user PATH but agent-sync did not resolve in a fresh process (execution-policy or PATHEXT association?) -- run agent-sync apply, then open a NEW terminal"
  } else {
    bad "$localBin missing from the user PATH -- run agent-sync apply, then open a NEW terminal"
  }
}

sec "Architecture Mode (99-INDEX/USER-PROFILE.md)"
# Mode is a MINIMUM baseline the user commits to, never a ceiling (see the
# clarifying paragraph in USER-PROFILE.md itself). It only gates whether a
# missing/unreachable connector below counts as a real FAIL: a connector is
# "expected" when Mode declares CLOUD-SERVER, OR when its own env var
# (matching manifest.yaml's require_env) is already set regardless of Mode --
# so an upgrade past the declared Mode, or a single cloud connector added
# while staying Local-Only for the rest, is recognized automatically and
# never punished for going beyond what Mode declares. A missing/unparseable
# Mode is treated as unknown (same gating as Local-Only, never a crash).
$UserProfileMd = Join-Path $Vault "99-INDEX\USER-PROFILE.md"
$DeclaredMode = "unknown"
if (Test-Path -LiteralPath $UserProfileMd) {
  $modeLine = Select-String -LiteralPath $UserProfileMd -Pattern '\*\*Mode\*\*' | Select-Object -First 1
  if ($modeLine) {
    $modeValue = ($modeLine.Line -replace '.*\*\*Mode\*\*\s*:\s*', '') -replace '[`\[\]]', ''
    $modeValue = $modeValue.Trim().ToUpperInvariant()
    switch ($modeValue) {
      "LOCAL-ONLY"   { $DeclaredMode = "local-only" }
      "CLOUD-SERVER" { $DeclaredMode = "cloud-server" }
      default        { $DeclaredMode = "unknown" }
    }
  }
}
switch ($DeclaredMode) {
  "local-only"   { ok "USER-PROFILE.md declares Mode: LOCAL-ONLY" }
  "cloud-server" { ok "USER-PROFILE.md declares Mode: CLOUD-SERVER" }
  default        { warn "USER-PROFILE.md Mode not found or not parseable ($UserProfileMd) -- treated as unknown (same gating as Local-Only unless a connector's own env var is already set)" }
}
function Test-ConnectorExpected([string]$VarName) {
  # A connector is "expected" (missing/unreachable is a real FAIL) when Mode
  # declares CLOUD-SERVER, OR when its own env var (see manifest.yaml's
  # require_env) is already set regardless of Mode.
  if ($DeclaredMode -eq "cloud-server") { return $true }
  return [bool][Environment]::GetEnvironmentVariable($VarName)
}

sec "MCP connectors - reachability"
$c = httpcode "http://127.0.0.1:5678/healthz" $null
if ($c -eq 200) { ok "n8n-mcp (5678): $c" }
elseif (Test-ConnectorExpected "N8N_MCP_TOKEN") { bad "n8n-mcp (5678): $c" }
else { ok "n8n-mcp (5678): not reachable ($c) - not expected in current Mode (Local-Only / N8N_MCP_TOKEN not set)" }
$c = httpcode "http://127.0.0.1:33002/" $null
if ($c -eq 200 -or $c -eq 302) { ok "firecrawl (33002): $c" }
elseif (Test-ConnectorExpected "FIRECRAWL_TUNNEL_PORT") { bad "firecrawl (33002): $c" }
else { ok "firecrawl (33002): not reachable ($c) - not expected in current Mode (Local-Only / FIRECRAWL_TUNNEL_PORT not set)" }
$c = httpcode "http://127.0.0.1:33003/health" $null
if ($c -eq 200) { ok "vault-ocr (33003): $c" }
elseif (Test-ConnectorExpected "OCR_TUNNEL_PORT") { bad "vault-ocr (33003): $c" }
else { ok "vault-ocr (33003): not reachable ($c) - not expected in current Mode (Local-Only / OCR_TUNNEL_PORT not set)" }
if ($env:VAULT_LIBRARY_URL) {
  # Streamable HTTP MCP rejects a generic GET without its protocol Accept
  # header. OPTIONS is a bounded, authenticated route probe.
  $c = httpcode $env:VAULT_LIBRARY_URL @{ Authorization = "Bearer $($env:VAULT_LIBRARY_TOKEN)"; Accept = "application/json, text/event-stream" } "Options"
  if ($c -eq 200 -or $c -eq 405) { ok "vault-library: $c (up)" } else { bad "vault-library: $c" }
} else { warn "VAULT_LIBRARY_URL not in env" }
if (Get-Command npx -ErrorAction SilentlyContinue) { ok "playwright: npx available" } else { warn "npx not in PATH (playwright MCP)" }

sec "Tokens in env"
if ([Environment]::GetEnvironmentVariable("N8N_MCP_TOKEN")) { ok "N8N_MCP_TOKEN present" }
elseif (Test-ConnectorExpected "N8N_MCP_TOKEN") { bad "N8N_MCP_TOKEN missing" }
else { ok "N8N_MCP_TOKEN not set - not expected in current Mode (Local-Only / n8n not configured)" }
foreach ($v in @("VAULT_LIBRARY_TOKEN","VAULT_LIBRARY_URL")) {
  if ([Environment]::GetEnvironmentVariable($v)) { ok "$v present" }
  elseif (Test-ConnectorExpected "VAULT_LIBRARY_URL") { bad "$v missing" }
  else { ok "$v not set - not expected in current Mode (Local-Only / vault-library not configured)" }
}
if ($env:DEEPSEEK_API_KEY) { ok "DEEPSEEK_API_KEY present" } else { warn "DEEPSEEK_API_KEY missing (OpenCode's default DeepSeek won't start)" }

sec "MCP configured in the runtimes (Vault 2.0 drift detection)"
if ($NexgenPython -and (Test-Path -LiteralPath $RenderPy)) {
  $renderOut = & $NexgenPythonCommand @NexgenPythonPrefix $RenderPy 2>&1
  if ($LASTEXITCODE -ne 0) {
    # A crash here (missing PyYAML, a broken manifest, a permission error...)
    # must never read as "no drift found": empty/error output would otherwise
    # fall through to the "100% aligned" branch below, the worst possible
    # false-green in the one check whose job is to catch misconfiguration.
    # cmd_diff now isolates a broken CLI PER SECTION instead of aborting on
    # the first one (2026-07-13 follow-up): the actual reason is one or more
    # '>>> STOP: ...' lines, not necessarily the last line of output (which
    # can just be the trailing summary) -- surface those specifically when
    # present, fall back to the last line only when there's no STOP marker
    # to show (e.g. a manifest-load crash outside the per-CLI loop).
    $stopLines = @($renderOut | Where-Object { $_ -match '>>> STOP' })
    if ($stopLines.Count -gt 0) {
      bad "render.py failed to run (exit $LASTEXITCODE): $($stopLines -join '; ')"
    } else {
      $lastLine = ($renderOut | Select-Object -Last 1)
      bad "render.py failed to run (exit $LASTEXITCODE): $lastLine"
    }
  } else {
  $driftLines = @($renderOut | Where-Object { $_ -match '\[DIFF\]|\[MISSING\]|\[ERROR\]' })
  if ($driftLines.Count -gt 0) {
    if (($renderOut | Where-Object { $_ -match '\[ERROR\]' }).Count -gt 0) {
      bad "MCP drift detected against the canonical manifest (ERROR)"
      warn "MCP drift detail: $($driftLines.Count) entries"
    } else {
      warn "MCP drift: $($driftLines.Count) render.py entries (partly expected on Windows; detail: python $RenderPy)"
    }
  } else {
    ok "MCP configs 100% aligned with the canonical manifest"
  }
  # A CLI that was never launched has no config file to patch: render.py just
  # notes "(config not present...)" for it, with no [DIFF]/[MISSING] tag, so
  # the drift scan above reads as clean even though that CLI has zero MCP
  # servers mounted. OpenCode is already caught above (missing $OcJson is a
  # hard fail there because it holds both instructions and MCP config); check
  # the other three here so this doesn't stay a silent gap.
  $renderText = ($renderOut | Out-String)
  foreach ($cli in @("claude", "codex", "antigravity")) {
    $upper = $cli.ToUpperInvariant()
    $m = [regex]::Match($renderText, "(?ms)^========== $upper ==========\r?\n(.*?)(?=^==========|\z)")
    if ($m.Success -and $m.Groups[1].Value -match "config not present") {
      $have = $false
      switch ($cli) {
        "claude" { $have = [bool](Get-Command claude -ErrorAction SilentlyContinue) }
        "codex" { $have = [bool](Get-Command codex -ErrorAction SilentlyContinue) }
        "antigravity" { $have = Test-Path -LiteralPath (Join-Path $HomeDir ".gemini") }
      }
      if ($have) { warn "$cli is installed but has never been launched: its MCP config doesn't exist yet, open it once and re-run agent-sync.ps1" }
    }
  }
  }
} else {
  warn "python or render.py not found, skipping MCP drift check"
}

if ($Strict) {
  sec "CLI consumer conformance (--strict)"
  # Found missing entirely in a cross-vendor audit, 2026-07-09: bash had this
  # since before, .ps1 never got a twin, so Windows never ran the real
  # consumer checks (only the structural ones above). Ported here as closely
  # as possible without a live Windows machine to verify against -- the OCR
  # JSONL/Content-Length framing check from agent-doctor.sh is intentionally
  # NOT ported: too easy to get subtly wrong blind, flag it in the backlog
  # instead of shipping an unverified process-framing test.
  # Expected MCP server set for the strict consumer checks below: used to be
  # a single hardcoded 4-name array (firecrawl / n8n-mcp / vault-library /
  # vault-ocr) reused for both Antigravity and OpenCode, so a manifest
  # change (server added/removed, a require_env gate flipped) silently went
  # stale, and a CLI-specific target (e.g. a server that only targets one
  # of the two) would have been checked against the wrong list. Derived
  # per-CLI instead, via render.py --expected-servers (same require_env
  # filtering agent-sync's --write uses -- see agent-doctor.sh's twin for
  # the parity rationale). An empty result (e.g. Local-Only) is legitimate
  # and different from "couldn't derive it" -- both skip the checks below
  # explicitly (never silently pass on an empty expected set, never fail).
  $expectedAg = @()
  $expectedOc = @()
  if (-not $NexgenPython) {
    warn "Python 3 with PyYAML not found -- cannot derive the expected MCP server set, skipping its strict checks"
  } elseif (-not (Test-Path -LiteralPath $RenderPy)) {
    warn "render.py not found ($RenderPy) -- cannot derive the expected MCP server set, skipping its strict checks"
  } else {
    $expectedAg = @(& $NexgenPythonCommand @NexgenPythonPrefix $RenderPy --expected-servers antigravity 2>$null | Where-Object { $_ })
    $expectedOc = @(& $NexgenPythonCommand @NexgenPythonPrefix $RenderPy --expected-servers opencode 2>$null | Where-Object { $_ })
  }

  $AgSrc = Join-Path $HomeDir ".gemini\antigravity\mcp_config.json"
  $AgGlobal = Join-Path $HomeDir ".gemini\config\mcp_config.json"
  if ((Test-Path -LiteralPath $AgGlobal) -and (Test-Path -LiteralPath $AgSrc) -and ((hashOf $AgGlobal) -eq (hashOf $AgSrc))) {
    ok "Antigravity global MCP path -> generated source"
  } else {
    bad "Antigravity global MCP path does NOT point to the generated source ($AgGlobal)"
  }
  try {
    $AgGlobalContent = if (Test-Path -LiteralPath $AgGlobal) { [IO.File]::ReadAllText($AgGlobal) } else { "" }
  } catch { $AgGlobalContent = "" }
  if ($AgGlobalContent.Length -gt 0) { ok "Antigravity global mcp_config.json not empty" }
  else { bad "Antigravity global mcp_config.json empty or missing" }
  if ($expectedAg.Count -eq 0) {
    warn "no expected Antigravity MCP servers derived from the manifest -- skipping the core-servers content check"
  } elseif (Test-Path -LiteralPath $AgGlobal) {
    try {
      $agJson = Get-Content -Raw -LiteralPath $AgGlobal | ConvertFrom-Json
      if ($agJson.mcpServers) {
        $gotKeys = @($agJson.mcpServers.PSObject.Properties.Name)
      } else {
        $gotKeys = @()
      }
      $agMissing = @($expectedAg | Where-Object { $gotKeys -notcontains $_ })
      if ($agMissing.Count -eq 0) { ok "Antigravity global contains the core MCP servers" }
      else { bad "Antigravity global is missing core MCP servers: $($agMissing -join ', ')" }
    } catch { bad "Antigravity global mcp_config.json: invalid JSON" }
  }
  # Real behavioral probe: agy has no deterministic "mcp list" subcommand
  # like opencode, so the only real check is asking the model itself.
  if ($expectedAg.Count -eq 0) {
    warn "no expected Antigravity MCP servers derived from the manifest -- skipping the Antigravity behavioral probe"
  } elseif (Get-Command agy -ErrorAction SilentlyContinue) {
    $agPrompt = "Elenca SOLO i nomi dei server MCP disponibili in questa sessione, una riga per server, NESSUN dettaglio sui singoli tool e NESSUNA invocazione."
    $agJob = Start-Job -ScriptBlock { param($p) & agy --print $p --model "Gemini 3.5 Flash (Medium)" --sandbox 2>&1 } -ArgumentList $agPrompt
    if (Wait-Job $agJob -Timeout 45) {
      $agProbeOut = (Receive-Job $agJob | Out-String)
      Remove-Job $agJob -Force
      if ($agProbeOut -match '(?i)individual\s+quota|quota\s+(reached|exhausted|exceeded)|rate\s+limit|too many requests|\b429\b') {
        warn "Antigravity behavioral probe skipped: the selected model quota is unavailable"
      }
      else {
        $agProbeMissing = @($expectedAg | Where-Object { $agProbeOut -notmatch [regex]::Escape($_) })
        if ($agProbeMissing.Count -eq 0) { ok "Antigravity behavioral probe confirms the core MCP servers are visible" }
        else { bad "Antigravity behavioral probe does not confirm: $($agProbeMissing -join ', ')" }
      }
    } else {
      Stop-Job $agJob; Remove-Job $agJob -Force
      bad "Antigravity behavioral probe (agy --print) timed out"
    }
  } else {
    warn "agy not in PATH, skipping Antigravity behavioral probe"
  }
  if ($expectedOc.Count -eq 0) {
    warn "no expected OpenCode MCP servers derived from the manifest -- skipping the OpenCode consumer test"
  } elseif (Get-Command opencode -ErrorAction SilentlyContinue) {
    $ocJob = Start-Job -ScriptBlock { & opencode mcp list 2>&1 }
    if (Wait-Job $ocJob -Timeout 25) {
      $ocOut = (Receive-Job $ocJob | Out-String)
      Remove-Job $ocJob -Force
      $ocMissing = @($expectedOc | Where-Object { $ocOut -notmatch "(?i)$([regex]::Escape($_)).*connected" })
      if ($ocMissing.Count -eq 0) { ok "OpenCode mcp list shows the core servers connected" }
      else { bad "OpenCode mcp list does not confirm: $($ocMissing -join ', ')" }
    } else {
      Stop-Job $ocJob; Remove-Job $ocJob -Force
      bad "OpenCode mcp list timed out during strict check"
    }
  } else {
    warn "opencode not in PATH, skipping OpenCode consumer test"
  }
  warn "OCR JSONL/Content-Length framing check not ported to Windows yet (see agentic-layer-concept-map.md backlog)"
}

sec "Skills"
$skActive = Join-Path $HomeDir ".agents\skills"
$skLibrary = Join-Path $HomeDir ".agents\skill-library"
$libraryEntries = if (Test-Path -LiteralPath $skLibrary) { @(Get-ChildItem -LiteralPath $skLibrary -Directory -Force) } else { @() }
$brokenLibrary = @(
  $libraryEntries | Where-Object {
    (($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) -and
      -not (Test-Path -LiteralPath $_.FullName)
  }
)
$managed = @(
  $libraryEntries | Where-Object {
    (Test-Path -LiteralPath $_.FullName) -and
      (Test-Path -LiteralPath (Join-Path $_.FullName "SKILL.md") -PathType Leaf)
  }
)
if ($managed.Count -gt 0) { ok "$($managed.Count) managed skills in ~/.agents/skill-library" } else { warn "no managed skill in ~/.agents/skill-library (fresh install, or none configured in the manifest yet)" }
if ($brokenLibrary.Count -gt 0) { bad "broken skill-library entries: $($brokenLibrary.Name -join ', ') — run: agent-sync guard" }
if (Test-Path -LiteralPath (Join-Path $skActive "INDEX.md") -PathType Leaf) { ok "lazy skill catalog present in ~/.agents/skills/INDEX.md" } else { warn "lazy skill catalog missing — run: agent-sync guard" }
$core = if (Test-Path -LiteralPath $skActive) {
  @(
    Get-ChildItem -LiteralPath $skActive -Directory |
      Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "SKILL.md") -PathType Leaf }
  ).Count
} else { 0 }
ok "$core skill folder(s) present in the shared discovery root; manifest reconciliation follows"
# Manifest -> library coverage: without this assert, a skill registered in the
# manifest can go missing on a host for weeks (the humanizer bug).
$skillsSyncScript = Join-Path $PSScriptRoot "skills-sync.py"
if ($NexgenPython -and (Test-Path -LiteralPath $skillsSyncScript)) {
  $ssOut = & $NexgenPythonCommand @NexgenPythonPrefix $skillsSyncScript 2>$null
  $ssExit = $LASTEXITCODE
  $esc = [char]27
  $clean = @($ssOut | ForEach-Object { "$_" -replace "$esc\[[0-9;]*m", "" })
  $pending = @($clean | Where-Object { $_ -match '^\s*\+ ' }).Count
  $manualFolders = @($clean | Where-Object { $_ -match 'manual but exists as a real folder' }).Count
  if ($ssExit -ne 0) { warn "skills-sync diff returned FAIL, check by hand" }
  elseif ($pending -gt 0) { warn "skill drift: $pending pending actions from the manifest (skills-sync --apply)" }
  elseif ($manualFolders -gt 0) { warn "$manualFolders manual skill folder(s) remain in discovery roots; preview the explicit quarantine with: skills-sync.py --migrate-legacy" }
  else { ok "skills aligned with the manifest (clean diff)" }

  $legacyOut = & $NexgenPythonCommand @NexgenPythonPrefix $skillsSyncScript --migrate-legacy 2>$null
  $legacyExit = $LASTEXITCODE
  $legacyClean = @($legacyOut | ForEach-Object { "$_" -replace "$esc\[[0-9;]*m", "" })
  $legacyPending = @($legacyClean | Where-Object { $_ -match '^\s*\+ legacy/' }).Count
  if ($legacyExit -ne 0) { warn "legacy skill migration preview returned FAIL, check by hand" }
  elseif ($legacyPending -gt 0) { warn "$legacyPending legacy eager skill view(s) await explicit quarantine: skills-sync.py --apply --migrate-legacy" }
  else { ok "no legacy eager skill views awaiting quarantine" }
} else { warn "python or skills-sync.py not available, skipping skill coverage" }

# Third-party CLI compatibility: a short, pruneable list of known-broken
# releases. NOT a general version pin -- only versions confirmed broken here
# (verified live: every tool call, including a no-op, was rejected with
# "unsupported call") get listed. Remove an entry once you've confirmed the
# upstream release fixed it; this list is expected to go stale and shrink.
# Same list as agent-doctor.sh -- was missing entirely from this file
# (beta-readiness review, 2026-07-13), a Windows user got no warning at all
# for a regression the Linux/Mac doctor already caught.
sec "Third-party CLI compatibility"
if (Get-Command codex -ErrorAction SilentlyContinue) {
  $codexVerRaw = & codex --version 2>$null
  $codexVer = ($codexVerRaw | Select-String -Pattern '\d+\.\d+\.\d+' | ForEach-Object { $_.Matches[0].Value } | Select-Object -First 1)
  if ($codexVer -eq '0.143.0') {
    bad "Codex CLI $codexVer has a known tool-dispatcher regression (every tool call is rejected as 'unsupported call') -- known-bad as of 2026-07-09, upgrade or downgrade past it. Check https://github.com/openai/codex/releases before assuming this is still accurate."
  } elseif ($codexVer) {
    ok "Codex CLI $codexVer (not in the known-bad list)"
  }
  # else: codex present but --version didn't parse a semver -- don't guess.
}

sec "OpenCode config"
if (Test-Path -LiteralPath $OcJson) {
  try { Get-Content -Raw -LiteralPath $OcJson | ConvertFrom-Json | Out-Null
    ok "opencode.json: valid JSON"
    if (Select-String -Quiet -LiteralPath $OcJson -Pattern "opencode(-go)?/deepseek-v4-pro") { ok "default = deepseek-v4-pro via Go" } else { warn "default model is not deepseek-v4-pro (Go)" }
  } catch { bad "opencode.json: invalid JSON" }
}

sec "Local model (host-aware: routing worker only on Windows, tag chosen locally)"
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if ($ollama) {
  # Same resolution order as local-model-agent.ps1: env -> local unsynced
  # file. No hardcoded model-name fallback: the local worker's model choice
  # is entirely per-user (see LOCAL-WORKER.md), never imposed by this doctor.
  $workerModel = $env:LOCAL_WORKER_MODEL
  if (-not $workerModel) {
    $mf = Join-Path $HomeDir ".config\local-worker\model"
    if (Test-Path -LiteralPath $mf) { $workerModel = (Get-Content -LiteralPath $mf -TotalCount 1).Trim() }
  }
  if (-not $workerModel) {
    warn "local worker: no model configured (set LOCAL_WORKER_MODEL or ~\.config\local-worker\model) -- skipping presence check"
  } else {
    $models = (& ollama list 2>$null) -join "`n"
    if ($models -match [regex]::Escape($workerModel)) { ok "local worker '$workerModel' present in ollama list" } else { warn "local worker '$workerModel' not in ollama list (config: ~\.config\local-worker\model or LOCAL_WORKER_MODEL)" }
  }
} else { warn "ollama not in PATH (local worker unavailable)" }

sec "Claude hooks (vault checkpoint/briefing)"
$settingsPath = Join-Path $HomeDir ".claude\settings.json"
if (Test-Path -LiteralPath $settingsPath) {
  try {
    $sj = Get-Content -Raw -LiteralPath $settingsPath | ConvertFrom-Json
    $cmds = @()
    foreach ($evt in @("SessionStart", "PreCompact")) {
      foreach ($m in @($sj.hooks.$evt)) { $cmds += @($m.hooks).command }
    }
    if (@($cmds | Where-Object { $_ -match "claude-vault-checkpoint" }).Count -ge 2) {
      ok "checkpoint/briefing hook on SessionStart + PreCompact"
    }
    else { bad "vault-checkpoint hook missing in settings.json (SessionStart/PreCompact) - run agent-sync.ps1" }
  }
  catch { bad "Claude settings.json: invalid JSON" }
}
else { warn "Claude settings.json missing (Claude not installed here?)" }

# Consumer engine clone - version pin (S2). Applies only where a consumer
# clone exists (default %USERPROFILE%\.nexgen-engine, or AGENT_ENGINE_ROOT's
# repo root). Before the cutover this machine has none, so the whole section
# is skipped silently - same pattern as the S0 section above.
$ConsumerEngineRoot = if ($env:AGENT_ENGINE_ROOT) { $env:AGENT_ENGINE_ROOT } else { Join-Path $HomeDir ".nexgen-engine\03-INFRA" }
$ConsumerEngineRepo = Split-Path -Parent $ConsumerEngineRoot
if (Test-Path -LiteralPath (Join-Path $ConsumerEngineRepo ".git")) {
  sec "Consumer engine clone - version pin (S2)"
  $pinFile = Join-Path $Vault "99-INDEX\ENGINE-PIN.txt"
  $liveSha = (& git -C $ConsumerEngineRepo rev-parse HEAD 2>$null)
  if (-not $liveSha) { bad "cannot read the consumer engine clone's HEAD ($ConsumerEngineRepo)" }
  elseif (Test-Path -LiteralPath $pinFile) {
    $pinSha = (Get-Content -LiteralPath $pinFile -TotalCount 1).Trim()
    $liveShort = if ($liveSha.Length -ge 7) { $liveSha.Substring(0,7) } else { $liveSha }
    $pinShort = if ($pinSha.Length -ge 7) { $pinSha.Substring(0,7) } else { $pinSha }
    if ($pinSha -eq $liveSha) { ok "consumer engine at the pinned version ($liveShort)" }
    else { bad "consumer engine at $liveShort, pin expects $pinShort - silent drift: pull was skipped, or the pin wasn't updated after a deliberate upgrade" }
  } else { warn "no engine pin set ($pinFile missing) - consumer engine version isn't tracked yet" }
  # New-version-available check (B3, informational only, never auto-updates).
  # Fetch is read-only (only moves remote-tracking refs/tags), safe even
  # though this machine never auto-upgrades the pinned commit.
  & git -C $ConsumerEngineRepo fetch --quiet --tags origin 2>$null | Out-Null
  $latestTag = (& git -C $ConsumerEngineRepo tag --merged origin/main --sort=-v:refname 2>$null | Select-Object -First 1)
  if ($latestTag -and $liveSha) {
    $currentVersion = (& git -C $ConsumerEngineRepo show "${liveSha}:VERSION" 2>$null)
    if ($currentVersion) { $currentVersion = $currentVersion.Trim() }
    if ($currentVersion) {
      if ("v$currentVersion" -ne $latestTag) { warn "new engine version available: $latestTag (pinned: v$currentVersion) - see docs/upgrade.md, update is always deliberate" }
      else { ok "consumer engine at the latest released version ($latestTag)" }
    } else { warn "new engine version available: $latestTag (pinned commit predates the VERSION file) - see docs/upgrade.md" }
  }
}

# New-version-available check for the DEFAULT single-clone install - twin of
# the bash check: informational only, never auto-updates, upgrading stays a
# deliberate act (docs/upgrade.md). Gated on the consumer clone NOT existing
# (no double warning after the cutover) and on this vault actually tracking
# the engine (a VERSION file at its root - a pure data vault has none).
if (-not (Test-Path -LiteralPath (Join-Path $ConsumerEngineRepo ".git")) -and
    (Test-Path -LiteralPath (Join-Path $Vault ".git")) -and
    (Test-Path -LiteralPath (Join-Path $Vault "VERSION"))) {
  sec "Engine version (single-clone install)"
  $currentVersion = (Get-Content -LiteralPath (Join-Path $Vault "VERSION") -TotalCount 1).Trim()
  & git -C $Vault fetch --quiet --tags origin 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {
    $latestTag = (& git -C $Vault tag --merged origin/main --sort=-v:refname 2>$null | Select-Object -First 1)
    if (-not $latestTag) {
      ok "origin has no released engine tags - nothing to compare (origin is not the engine repo?)"
    } else {
      # Semantic comparison so a clone sitting AHEAD of the last tag doesn't
      # get told to "upgrade" backwards; falls back to string inequality if
      # either side isn't parseable as a version.
      $cur = $null; $lat = $null
      $curOk = [System.Version]::TryParse($currentVersion, [ref]$cur)
      $latOk = [System.Version]::TryParse(($latestTag -replace '^v', ''), [ref]$lat)
      if ($curOk -and $latOk) {
        if ($lat -gt $cur) { warn "new engine version available: $latestTag (running: v$currentVersion) - see docs/upgrade.md, update is always deliberate" }
        else { ok "engine at (or ahead of) the latest released version ($latestTag, running v$currentVersion)" }
      } elseif ("v$currentVersion" -ne $latestTag) {
        warn "new engine version available: $latestTag (running: v$currentVersion) - see docs/upgrade.md, update is always deliberate"
      } else {
        ok "engine at the latest released version ($latestTag)"
      }
    }
  } else { warn "cannot fetch origin - engine version check skipped (offline, or origin unreachable)" }
}

if ($Summary) {
  $line = "agent-doctor [windows] PASS=$($script:PASS) WARN=$($script:WARN) FAIL=$($script:FAILN)"
  if ($script:FAILN -gt 0) { $line += " | FAIL: " + ($script:FAILS -join ', ') }
  Write-Output $line
} else {
  sec "Summary"
  Write-Host ("  PASS={0}  WARN={1}  FAIL={2}" -f $script:PASS, $script:WARN, $script:FAILN)
  if ($script:FAILN -eq 0) { Write-Host "  -> alignment VERIFIED" -ForegroundColor Green } else { Write-Host "  -> there are FAILs to fix" -ForegroundColor Red }
}
if ($script:FAILN -eq 0) { exit 0 } else { exit 1 }
