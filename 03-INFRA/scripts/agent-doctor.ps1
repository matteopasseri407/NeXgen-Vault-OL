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
$Canon   = Join-Path $Layer "instructions\AGENTS.md"
$OcJson  = Join-Path $HomeDir ".config\opencode\opencode.json"

$RemoteConfigError = $false
if ($env:KNOWLEDGE_VAULT_REMOTE) {
  $Remote = $env:KNOWLEDGE_VAULT_REMOTE
  $Mirrors = if ($env:KNOWLEDGE_VAULT_MIRRORS) { @($env:KNOWLEDGE_VAULT_MIRRORS -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) } else { @() }
} else {
  $AgentSyncPy = Join-Path $PSScriptRoot "agent_sync.py"
  $Py = Get-Command py -ErrorAction SilentlyContinue
  if ($Py) {
    $Remote = (& $Py.Source -3 $AgentSyncPy config authoritative_remote 2>$null)
    $RemoteExit = $LASTEXITCODE
    $Mirrors = @(& $Py.Source -3 $AgentSyncPy config mirrors 2>$null | Where-Object { $_ })
    $MirrorsExit = $LASTEXITCODE
  } else {
    $Python = Get-Command python -ErrorAction SilentlyContinue
    if ($Python) {
      $Remote = (& $Python.Source $AgentSyncPy config authoritative_remote 2>$null)
      $RemoteExit = $LASTEXITCODE
      $Mirrors = @(& $Python.Source $AgentSyncPy config mirrors 2>$null | Where-Object { $_ })
      $MirrorsExit = $LASTEXITCODE
    } else {
      $RemoteConfigError = $true
      $Remote = "origin"
      $Mirrors = @()
      $RemoteExit = 1
      $MirrorsExit = 1
    }
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

sec "Vault (memory) - authoritative remote and mirrors"
if ($RemoteConfigError) { bad "invalid sync remote config - run: agent-sync config authoritative_remote" } else { ok "sync remote config resolved ($Remote)" }
if (Test-Path -LiteralPath (Join-Path $Vault ".git")) {
  gitc @("fetch","--prune",$Remote,$Branch) | Out-Null
  $b = (gitc @("rev-list","--count","$Branch..$Remote/$Branch")); if (-not $b) { $b = "?" }
  $a = (gitc @("rev-list","--count","$Remote/$Branch..$Branch")); if (-not $a) { $a = "?" }
  $d = @(gitc @("status","--porcelain","--untracked-files=no")).Where({ $_ }).Count
  if ("$b" -eq "0") { ok "aligned with $Remote/$Branch (0 behind)" } else { bad "$b commits behind the cloud" }
  # TODO(2026-07-10): Alert per dangling commit (da riguardare in review)
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

sec "MCP connectors - reachability"
$c = httpcode "http://127.0.0.1:5678/healthz" $null; if ($c -eq 200) { ok "n8n-mcp (5678): $c" } else { bad "n8n-mcp (5678): $c" }
$c = httpcode "http://127.0.0.1:33002/" $null; if ($c -eq 200 -or $c -eq 302) { ok "firecrawl (33002): $c" } else { bad "firecrawl (33002): $c" }
$c = httpcode "http://127.0.0.1:33003/health" $null; if ($c -eq 200) { ok "vault-ocr (33003): $c" } else { bad "vault-ocr (33003): $c" }
if ($env:VAULT_LIBRARY_URL) {
  # Streamable HTTP MCP rejects a generic GET without its protocol Accept
  # header. OPTIONS is a bounded, authenticated route probe.
  $c = httpcode $env:VAULT_LIBRARY_URL @{ Authorization = "Bearer $($env:VAULT_LIBRARY_TOKEN)"; Accept = "application/json, text/event-stream" } "Options"
  if ($c -eq 200 -or $c -eq 405) { ok "vault-library: $c (up)" } else { bad "vault-library: $c" }
} else { warn "VAULT_LIBRARY_URL not in env" }
if (Get-Command npx -ErrorAction SilentlyContinue) { ok "playwright: npx available" } else { warn "npx not in PATH (playwright MCP)" }

sec "Tokens in env"
foreach ($v in @("N8N_MCP_TOKEN","VAULT_LIBRARY_TOKEN","VAULT_LIBRARY_URL")) {
  if ([Environment]::GetEnvironmentVariable($v)) { ok "$v present" } else { bad "$v missing" }
}
if ($env:DEEPSEEK_API_KEY) { ok "DEEPSEEK_API_KEY present" } else { warn "DEEPSEEK_API_KEY missing (OpenCode's default DeepSeek won't start)" }

sec "MCP configured in the runtimes (Vault 2.0 drift detection)"
if ((Get-Command "python" -ErrorAction SilentlyContinue) -and (Test-Path -LiteralPath "$Layer\mcp\render.py")) {
  $renderOut = python "$Layer\mcp\render.py" 2>&1
  if ($LASTEXITCODE -ne 0) {
    # A crash here (missing PyYAML, a broken manifest, a permission error...)
    # must never read as "no drift found": empty/error output would otherwise
    # fall through to the "100% aligned" branch below, the worst possible
    # false-green in the one check whose job is to catch misconfiguration.
    $lastLine = ($renderOut | Select-Object -Last 1)
    bad "render.py failed to run (exit $LASTEXITCODE): $lastLine"
  } else {
  $driftLines = @($renderOut | Where-Object { $_ -match '\[DIFF\]|\[MISSING\]|\[ERROR\]' })
  if ($driftLines.Count -gt 0) {
    if (($renderOut | Where-Object { $_ -match '\[ERROR\]' }).Count -gt 0) {
      bad "MCP drift detected against the canonical manifest (ERROR)"
      warn "MCP drift detail: $($driftLines.Count) entries"
    } else {
      warn "MCP drift: $($driftLines.Count) render.py entries (partly expected on Windows; detail: python `$Layer\mcp\render.py)"
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
  $AgSrc = Join-Path $HomeDir ".gemini\antigravity\mcp_config.json"
  $AgGlobal = Join-Path $HomeDir ".gemini\config\mcp_config.json"
  if ((Test-Path -LiteralPath $AgGlobal) -and (Test-Path -LiteralPath $AgSrc) -and ((hashOf $AgGlobal) -eq (hashOf $AgSrc))) {
    ok "Antigravity global MCP path -> generated source"
  } else {
    bad "Antigravity global MCP path does NOT point to the generated source ($AgGlobal)"
  }
  if ((Test-Path -LiteralPath $AgGlobal) -and (Get-Item -LiteralPath $AgGlobal).Length -gt 0) {
    ok "Antigravity global mcp_config.json not empty"
  } else {
    bad "Antigravity global mcp_config.json empty or missing"
  }
  $expectedMcp = @("firecrawl", "n8n-mcp", "vault-library", "vault-ocr")
  if (Test-Path -LiteralPath $AgGlobal) {
    try {
      $agJson = Get-Content -Raw -LiteralPath $AgGlobal | ConvertFrom-Json
      if ($agJson.mcpServers) {
        $gotKeys = @($agJson.mcpServers.PSObject.Properties.Name)
      } else {
        $gotKeys = @()
      }
      $agMissing = @($expectedMcp | Where-Object { $gotKeys -notcontains $_ })
      if ($agMissing.Count -eq 0) { ok "Antigravity global contains the core MCP servers" }
      else { bad "Antigravity global is missing core MCP servers: $($agMissing -join ', ')" }
    } catch { bad "Antigravity global mcp_config.json: invalid JSON" }
  }
  # Real behavioral probe: agy has no deterministic "mcp list" subcommand
  # like opencode, so the only real check is asking the model itself.
  if (Get-Command agy -ErrorAction SilentlyContinue) {
    $agPrompt = "Elenca SOLO i nomi dei server MCP disponibili in questa sessione, una riga per server, NESSUN dettaglio sui singoli tool e NESSUNA invocazione."
    $agJob = Start-Job -ScriptBlock { param($p) & agy --print $p --model "Gemini 3.5 Flash (Medium)" --sandbox 2>&1 } -ArgumentList $agPrompt
    if (Wait-Job $agJob -Timeout 45) {
      $agProbeOut = (Receive-Job $agJob | Out-String)
      Remove-Job $agJob -Force
      if ($agProbeOut -match '(?i)individual\s+quota|quota\s+(reached|exhausted|exceeded)|rate\s+limit|too many requests|\b429\b') {
        warn "Antigravity behavioral probe skipped: the selected model quota is unavailable"
      }
      else {
        $agProbeMissing = @($expectedMcp | Where-Object { $agProbeOut -notmatch [regex]::Escape($_) })
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
  if (Get-Command opencode -ErrorAction SilentlyContinue) {
    $ocJob = Start-Job -ScriptBlock { & opencode mcp list 2>&1 }
    if (Wait-Job $ocJob -Timeout 25) {
      $ocOut = (Receive-Job $ocJob | Out-String)
      Remove-Job $ocJob -Force
      $ocMissing = @($expectedMcp | Where-Object { $ocOut -notmatch "(?i)$([regex]::Escape($_)).*connected" })
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
ok "$core core skills exposed to eager runtimes"
# Manifest -> library coverage: without this assert, a skill registered in the
# manifest can go missing on a host for weeks (the humanizer bug).
$skillsSyncScript = Join-Path $Vault "03-INFRA\scripts\skills-sync.py"
if ((Get-Command "python" -ErrorAction SilentlyContinue) -and (Test-Path -LiteralPath $skillsSyncScript)) {
  $ssOut = & python $skillsSyncScript 2>$null
  $ssExit = $LASTEXITCODE
  $esc = [char]27
  $clean = @($ssOut | ForEach-Object { "$_" -replace "$esc\[[0-9;]*m", "" })
  $pending = @($clean | Where-Object { $_ -match '^\s*\+ ' }).Count
  if ($ssExit -ne 0) { warn "skills-sync diff returned FAIL, check by hand" }
  elseif ($pending -gt 0) { warn "skill drift: $pending pending actions from the manifest (skills-sync --apply)" }
  else { ok "skills aligned with the manifest (clean diff)" }
} else { warn "python or skills-sync.py not available, skipping skill coverage" }

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
  # Same resolution order as local-model-agent.ps1: env -> local unsynced file -> historical default.
  $workerModel = $env:LOCAL_WORKER_MODEL
  if (-not $workerModel) {
    $mf = Join-Path $HomeDir ".config\local-worker\model"
    if (Test-Path -LiteralPath $mf) { $workerModel = (Get-Content -LiteralPath $mf -TotalCount 1).Trim() }
  }
  if (-not $workerModel) { $workerModel = "gemma4-12b-128k" }
  $models = (& ollama list 2>$null) -join "`n"
  if ($models -match [regex]::Escape($workerModel)) { ok "local worker '$workerModel' present in ollama list" } else { warn "local worker '$workerModel' not in ollama list (config: ~\.config\local-worker\model or LOCAL_WORKER_MODEL)" }
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

# Public engine repo - anti-leak gates (S0). Maintainer lane: these checks
# only apply when explicitly enabled by a contributor/maintainer that publishes
# engine code. Normal end users never push this repo; they only publish their
# private vault data.
# Ported from agent-doctor.sh (bash never had a Windows twin for this).
$EngineRepo = if ($env:ENGINE_REPO) { $env:ENGINE_REPO } else { Join-Path $HomeDir "NeXgen-Engine" }
$EngineMaintainer = if ($env:NEXGEN_ENGINE_MAINTAINER) { $env:NEXGEN_ENGINE_MAINTAINER } else { "0" }
if (($EngineMaintainer -eq "1") -and (Test-Path -LiteralPath (Join-Path $EngineRepo ".git"))) {
  sec "Public engine repo - anti-leak gates (S0)"
  $pushUrl = (& git -C $EngineRepo config --get remote.origin.pushurl 2>$null)
  if ("$pushUrl".StartsWith("PUSH-DISABLED")) { ok "direct push disabled on the engine clone ($EngineRepo)" }
  else { bad "direct push NOT disabled on the engine clone: git -C $EngineRepo remote set-url --push origin PUSH-DISABLED-use-engine-push" }
  $fetchUrl = (& git -C $EngineRepo config --get remote.origin.url 2>$null)
  if (-not $fetchUrl -or "$fetchUrl".StartsWith("PUSH-DISABLED")) { bad "the engine clone's remote.origin.url is not a valid URL" }
  else { ok "engine clone fetch url intact" }
  $hooksSrc = Join-Path $Layer "sanitize\engine-hooks"
  if (Test-Path -LiteralPath $hooksSrc) {
    foreach ($h in @("pre-commit", "commit-msg")) {
      $installed = Join-Path $EngineRepo ".git\hooks\$h"
      $tracked = Join-Path $hooksSrc $h
      if ((Test-Path -LiteralPath $installed) -and (Test-Path -LiteralPath $tracked)) {
        if ((hashOf $installed) -eq (hashOf $tracked)) { ok "hook $h installed and aligned with its tracked source" }
        else { warn "hook $h installed but DIFFERENT from its tracked source (drift - reinstall from $tracked)" }
      } else { bad "hook $h missing from the engine clone (.git\hooks\$h) - reinstall from $tracked" }
    }
  } else { warn "anti-leak hook sources not found ($hooksSrc) - cannot verify the engine clone's hooks" }
  if (Get-Command engine-push -ErrorAction SilentlyContinue) { ok "engine-push available in PATH" }
  else { bad "engine-push not found in PATH - it is the only allowed push channel for the engine repo" }
}

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
  if ($EngineMaintainer -eq "1") {
    $pushUrl2 = (& git -C $ConsumerEngineRepo config --get remote.origin.pushurl 2>$null)
    if ("$pushUrl2".StartsWith("PUSH-DISABLED")) { ok "direct push disabled on the consumer engine clone" }
    else { bad "direct push NOT disabled on the consumer engine clone: git -C $ConsumerEngineRepo remote set-url --push origin PUSH-DISABLED-use-engine-push" }
  }

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
