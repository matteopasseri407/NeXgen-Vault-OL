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
param([switch]$Summary)

$ErrorActionPreference = "Continue"
$HomeDir = [Environment]::GetFolderPath("UserProfile")
$Vault   = if ($env:KNOWLEDGE_VAULT_PATH) { $env:KNOWLEDGE_VAULT_PATH } else { Join-Path $HomeDir "KnowledgeVault" }
$Remote  = if ($env:KNOWLEDGE_VAULT_REMOTE) { $env:KNOWLEDGE_VAULT_REMOTE } else { "origin" }
$Branch  = if ($env:KNOWLEDGE_VAULT_BRANCH) { $env:KNOWLEDGE_VAULT_BRANCH } else { "main" }
$Layer   = Join-Path $Vault "03-INFRA\agent-universal-layer"
$Canon   = Join-Path $Layer "instructions\AGENTS.md"
$OcJson  = Join-Path $HomeDir ".config\opencode\opencode.json"

$script:PASS = 0; $script:WARN = 0; $script:FAILN = 0; $script:FAILS = @()
function ok($m)   { $script:PASS++;  if (-not $Summary) { Write-Host "  [OK]   $m" -ForegroundColor Green } }
function warn($m) { $script:WARN++;  if (-not $Summary) { Write-Host "  [WARN] $m" -ForegroundColor Yellow } }
function bad($m)  { $script:FAILN++; $script:FAILS += $m; if (-not $Summary) { Write-Host "  [FAIL] $m" -ForegroundColor Red } }
function sec($m)  { if (-not $Summary) { Write-Host "`n$m" -ForegroundColor White } }
function gitc([string[]]$GitArgs) { (& git -C $Vault @GitArgs 2>$null) }
function httpcode($url, $headers) {
  try { (Invoke-WebRequest -Uri $url -Method Get -TimeoutSec 6 -Headers $headers -UseBasicParsing -ErrorAction Stop).StatusCode }
  catch { if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 } }
}
function hashOf($p) { if (Test-Path -LiteralPath $p) { (Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash } else { "" } }

if (-not $Summary) { Write-Host "=== agent-doctor: agent alignment check ===" -ForegroundColor White }

sec "Host"
ok "detected: windows ($env:COMPUTERNAME)"

sec "Vault (memory) - git vs remote hub + GitHub mirror"
if (Test-Path -LiteralPath (Join-Path $Vault ".git")) {
  gitc @("fetch","--prune",$Remote,$Branch) | Out-Null
  $b = (gitc @("rev-list","--count","$Branch..$Remote/$Branch")); if (-not $b) { $b = "?" }
  $a = (gitc @("rev-list","--count","$Remote/$Branch..$Branch")); if (-not $a) { $a = "?" }
  $d = @(gitc @("status","--porcelain","--untracked-files=no")).Where({ $_ }).Count
  if ("$b" -eq "0") { ok "aligned with $Remote/$Branch (0 behind)" } else { bad "$b commits behind the cloud" }
  if ("$a" -eq "0") { ok "no unpublished local commits" } else { warn "$a unpublished local commits" }
  if ($d -eq 0)     { ok "working tree clean (tracked files)" } else { warn "$d tracked files not committed (blocks the pull)" }
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
  $c = httpcode $env:VAULT_LIBRARY_URL @{ Authorization = "Bearer $($env:VAULT_LIBRARY_TOKEN)" }
  if ($c -ne 0 -and $c -ne 401 -and $c -ne 403) { ok "vault-library: $c (up)" } else { bad "vault-library: $c" }
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
  $driftLines = @($renderOut | Where-Object { $_ -match '\[DIFF\]|\[MISSING\]|\[ERROR\]' })
  if ($driftLines.Count -gt 0) {
    # render.py does not have a Windows dialect yet (paths/npx are expected in
    # Linux style): informational WARN, not FAIL, until Windows rendering is
    # implemented (Vault 2.0 backlog).
    warn "MCP drift: $($driftLines.Count) render.py entries (partly expected on Windows; detail: python `$Layer\mcp\render.py)"
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
} else {
  warn "python or render.py not found, skipping MCP drift check"
}

sec "Skills"
$sk = Join-Path $HomeDir ".agents\skills"
$n = if (Test-Path -LiteralPath $sk) { @(Get-ChildItem -LiteralPath $sk -Directory).Count } else { 0 }
if ($n -gt 0) { ok "$n skills in ~/.agents/skills" } else { bad "no skill in ~/.agents/skills" }
# Manifest -> hub coverage: without this assert, a skill registered in the
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
