[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("pull", "guard", "apply", "publish", "doctor", "full", "help")]
    [string]$Mode = "full",
    [string]$HomeDir = [Environment]::GetFolderPath("UserProfile"),
    [string]$VaultDir = $env:KNOWLEDGE_VAULT_PATH,
    [string]$Remote = $env:KNOWLEDGE_VAULT_REMOTE,
    [string]$Branch = $env:KNOWLEDGE_VAULT_BRANCH,
    [switch]$InstallScheduledTask,
    [switch]$SkipMcp
)

$ErrorActionPreference = "Stop"

if (-not $VaultDir) {
    $VaultDir = Join-Path $HomeDir "KnowledgeVault"
}
if (-not $Remote) {
    $Remote = "origin"
}
if (-not $Branch) {
    $Branch = "main"
}

$DataDir = if ($env:AGENT_VAULT_DATA) { $env:AGENT_VAULT_DATA } else { $VaultDir }
$EngineRoot = if ($env:AGENT_ENGINE_ROOT) { $env:AGENT_ENGINE_ROOT } else { Join-Path $VaultDir "03-INFRA" }

$LayerDir = Join-Path $EngineRoot "agent-universal-layer"
$CanonicalInstructions = Join-Path $LayerDir "instructions\AGENTS.md"
$CanonicalLocalWorkerInstructions = Join-Path $LayerDir "instructions\LOCAL-WORKER.md"
$CanonicalGemmaInstructions = Join-Path $LayerDir "instructions\GEMMA.md"
$CanonicalLocalModelScript = Join-Path $EngineRoot "scripts\local-model-agent.ps1"
$CanonicalAgentNowScript = Join-Path $EngineRoot "scripts\agent-now.ps1"
$UniversalSkillSourceRoot = Join-Path $LayerDir "skills"
$AgentSkillRoot = Join-Path $HomeDir ".agents\skills"
$LogDir = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "agent-sync"
}
else {
    Join-Path $HomeDir ".local\state"
}
$LogPath = Join-Path $LogDir "agent-sync.log"
$BackupStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$ScheduledTaskName = "KnowledgeVault Agent Sync"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$DoPull = $false
$DoApply = $false
$DoPush = $false
$DoCreds = $false
$DoHealth = $false

switch ($Mode) {
    "pull" {
        $DoPull = $true
        $DoHealth = $true
    }
    { $_ -in @("guard", "apply") } {
        $DoPull = $true
        $DoApply = $true
        $DoHealth = $true
    }
    "publish" {
        $DoPush = $true
    }
    "doctor" {
        $DoHealth = $true
    }
    "full" {
        $DoPull = $true
        $DoApply = $true
        $DoPush = $true
        $DoCreds = $true
        $DoHealth = $true
    }
    "help" {
        $helpText = @"
agent-sync.ps1 modes:
  pull     Pull the KnowledgeVault from the remote and run healthcheck. Does not rewrite CLI runtime files.
  guard    Recurring safe propagation: pull, regenerate CLI runtime files, run healthcheck. Does not push.
  apply    Same as guard, explicit manual name for provisioning.
  publish  Push already-committed local vault changes to the remote (and mirror origin if configured).
  doctor   Run healthcheck/alerts only.
  full     Legacy full run: pull, apply runtime files, publish, creds, healthcheck.

Default without arguments: full, for backward compatibility.
The recurring scheduled task should use: agent-sync.ps1 -Mode guard
"@
        Write-Host $helpText
        exit 0
    }
}

function Write-Log {
    param([string]$Message)
    $line = "{0:s} {1}" -f (Get-Date), $Message
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )
    $parent = Split-Path -Path $Path -Parent
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

function Invoke-GitCapture {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $oldErrorActionPreference = $ErrorActionPreference
    try {
        $script:ErrorActionPreference = "Continue"
        $output = & git -C $DataDir @Arguments 2>&1
        return [pscustomobject]@{
            ExitCode = $LASTEXITCODE
            Output = ($output | ForEach-Object { "$_" }) -join "`n"
        }
    }
    finally {
        $script:ErrorActionPreference = $oldErrorActionPreference
    }
}

function Get-GitText {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $result = Invoke-GitCapture -Arguments $Arguments
    if ($result.ExitCode -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $($result.ExitCode): $($result.Output)"
    }
    return $result.Output.Trim()
}

function Invoke-GitLogged {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $result = Invoke-GitCapture -Arguments $Arguments
    if ($result.Output) {
        Write-Log "git $($Arguments -join ' '): $($result.Output)"
    }
    if ($result.ExitCode -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $($result.ExitCode)"
    }
}

function Test-SameFileContent {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )
    if (-not (Test-Path -LiteralPath $Left) -or -not (Test-Path -LiteralPath $Right)) {
        return $false
    }
    $leftHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Left).Hash
    $rightHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Right).Hash
    return $leftHash -eq $rightHash
}

function Backup-Path {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $backup = "$Path.pre-agent-sync-$BackupStamp.bak"
    Copy-Item -LiteralPath $Path -Destination $backup -Recurse -Force
    Write-Log "backup: $Path -> $backup"
}

function Get-LinkTarget {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.LinkType -and $item.Target) {
        if ($item.Target -is [array]) {
            return $item.Target[0]
        }
        return [string]$item.Target
    }
    return ""
}

function Set-CanonicalFile {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Target
    )
    if (-not (Test-Path -LiteralPath $Source)) {
        Write-Log "instructions: missing source $Source"
        return
    }

    $parent = Split-Path -Path $Target -Parent
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }

    $existingTarget = Get-LinkTarget -Path $Target
    if ($existingTarget -and ((Resolve-Path -LiteralPath $existingTarget -ErrorAction SilentlyContinue).Path -eq (Resolve-Path -LiteralPath $Source).Path)) {
        return
    }

    $sameContent = Test-SameFileContent -Left $Source -Right $Target
    if ($sameContent -and -not $existingTarget) {
        return
    }
    if (Test-Path -LiteralPath $Target) {
        $item = Get-Item -LiteralPath $Target -Force
        if (-not $sameContent -and -not $item.LinkType) {
            Backup-Path -Path $Target
        }
        Remove-Item -LiteralPath $Target -Force
    }

    try {
        New-Item -ItemType SymbolicLink -Path $Target -Target $Source -Force | Out-Null
        Write-Log "instructions: linked $Target -> $Source"
    }
    catch {
        Copy-Item -LiteralPath $Source -Destination $Target -Force
        Write-Log "instructions: copied $Source -> $Target (symlink unavailable: $($_.Exception.Message))"
    }
}

function Set-ClaudePointerFile {
    param(
        [Parameter(Mandatory = $true)][string]$Canonical,
        [Parameter(Mandatory = $true)][string]$Target
    )
    if (-not (Test-Path -LiteralPath $Canonical)) {
        Write-Log "instructions: missing canonical Claude pointer source $Canonical"
        return
    }

    $parent = Split-Path -Path $Target -Parent
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }

    $content = @(
        "# Claude compatibility pointer",
        "",
        "Canonical instructions live at:",
        $Canonical,
        "",
        "At session start, read and follow that file when the user-specific agent policy is needed.",
        "Do not duplicate the full bootstrap in CLAUDE.md."
    ) -join [Environment]::NewLine
    $content = $content + [Environment]::NewLine

    $existingTarget = Get-LinkTarget -Path $Target
    $current = ""
    if (Test-Path -LiteralPath $Target) {
        $current = Get-Content -Raw -LiteralPath $Target -ErrorAction SilentlyContinue
    }
    if (-not $existingTarget -and $current -eq $content) {
        return
    }

    if (Test-Path -LiteralPath $Target) {
        $item = Get-Item -LiteralPath $Target -Force
        if (-not $existingTarget -and -not $item.LinkType -and $current -ne $content) {
            Backup-Path -Path $Target
        }
        Remove-Item -LiteralPath $Target -Force
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding -ArgumentList $false
    [System.IO.File]::WriteAllText($Target, $content, $utf8NoBom)
    Write-Log "instructions: wrote Claude pointer $Target -> $Canonical"
}

function Test-SameSkill {
    param(
        [Parameter(Mandatory = $true)][string]$SourceDir,
        [Parameter(Mandatory = $true)][string]$TargetDir
    )
    $sourceSkill = Join-Path $SourceDir "SKILL.md"
    $targetSkill = Join-Path $TargetDir "SKILL.md"
    return (Test-SameFileContent -Left $sourceSkill -Right $targetSkill)
}

function Set-CanonicalDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Target
    )
    if (-not (Test-Path -LiteralPath $Source)) {
        return
    }

    New-Item -ItemType Directory -Force -Path (Split-Path -Path $Target -Parent) | Out-Null
    $existingTarget = Get-LinkTarget -Path $Target
    if ($existingTarget -and ((Resolve-Path -LiteralPath $existingTarget -ErrorAction SilentlyContinue).Path -eq (Resolve-Path -LiteralPath $Source).Path)) {
        return
    }

    if (Test-Path -LiteralPath $Target) {
        $item = Get-Item -LiteralPath $Target -Force
        $sameSkill = Test-SameSkill -SourceDir $Source -TargetDir $Target
        if ($sameSkill -and -not $item.LinkType) {
            return
        }
        if (-not $sameSkill -and -not $item.LinkType) {
            Backup-Path -Path $Target
        }
        Remove-Item -LiteralPath $Target -Recurse -Force
    }

    try {
        New-Item -ItemType Junction -Path $Target -Target $Source -Force | Out-Null
        Write-Log "skill: junction $Target -> $Source"
    }
    catch {
        New-Item -ItemType Directory -Force -Path $Target | Out-Null
        Get-ChildItem -LiteralPath $Source -Force | Copy-Item -Destination $Target -Recurse -Force
        Write-Log "skill: copied $Source -> $Target (junction unavailable: $($_.Exception.Message))"
    }
}

function Invoke-BestEffortPull {
    if ($Remote -eq "local" -or $Remote -eq "none") {
        Write-Log "pull: skipped (Local-Only mode)"
        return
    }
    if (-not (Test-Path -LiteralPath (Join-Path $DataDir ".git"))) {
        Write-Log "pull: vault git repo not found at $DataDir"
        return
    }

    try {
        $status = Get-GitText -Arguments @("status", "--porcelain", "--untracked-files=no")
        if ($status) {
            Write-Log "pull: skipped because vault has uncommitted tracked changes (untracked files do not block)"
            return
        }

        $currentBranch = Get-GitText -Arguments @("rev-parse", "--abbrev-ref", "HEAD")
        if ($currentBranch -ne $Branch) {
            Invoke-GitLogged -Arguments @("switch", $Branch)
        }

        Invoke-GitLogged -Arguments @("fetch", "--prune", $Remote, $Branch)
        $localHead = Get-GitText -Arguments @("rev-parse", $Branch)
        $remoteHead = Get-GitText -Arguments @("rev-parse", "$Remote/$Branch")
        $mergeBase = Get-GitText -Arguments @("merge-base", $Branch, "$Remote/$Branch")

        if ($localHead -eq $remoteHead) {
            Write-Log "pull: already up to date"
        }
        elseif ($mergeBase -eq $localHead) {
            Invoke-GitLogged -Arguments @("merge", "--ff-only", "$Remote/$Branch")
            Write-Log "pull: fast-forwarded from $Remote/$Branch"
        }
        elseif ($mergeBase -eq $remoteHead) {
            Write-Log "pull: local branch ahead of $Remote/$Branch; skipping merge"
        }
        else {
            Write-Log "pull: local branch diverged from $Remote/$Branch; manual resolution required"
        }
    }
    catch {
        Write-Log "pull: cloud unavailable or not syncable; continuing with local copy ($($_.Exception.Message))"
    }
}

function Sync-Instructions {
    foreach ($target in @(
        (Join-Path $HomeDir ".codex\AGENTS.md"),
        (Join-Path $HomeDir ".gemini\config\AGENTS.md"),
        (Join-Path $HomeDir "ANTIGRAVITY.md")
    )) {
        Set-CanonicalFile -Source $CanonicalInstructions -Target $target
    }
    Set-ClaudePointerFile -Canonical $CanonicalInstructions -Target (Join-Path $HomeDir "CLAUDE.md")
    Set-CanonicalFile -Source $CanonicalGemmaInstructions -Target (Join-Path $HomeDir "GEMMA.md")
    Set-CanonicalFile -Source $CanonicalLocalWorkerInstructions -Target (Join-Path $HomeDir "LOCAL-WORKER.md")
}

function Sync-LocalModelRuntime {
    if (-not (Test-Path -LiteralPath $CanonicalLocalModelScript)) {
        Write-Log "local-model: missing source $CanonicalLocalModelScript"
        return
    }

    $localBin = Join-Path $HomeDir ".local\bin"
    New-Item -ItemType Directory -Force -Path $localBin | Out-Null

    $runtimeScript = Join-Path $localBin "local-model-agent.ps1"
    Set-CanonicalFile -Source $CanonicalLocalModelScript -Target $runtimeScript

    foreach ($oldShim in @(
        (Join-Path $localBin "gemma-worker.cmd"),
        (Join-Path $localBin "gemma-agent.cmd")
    )) {
        if (Test-Path -LiteralPath $oldShim) {
            Remove-Item -LiteralPath $oldShim -Force
        }
    }

    Write-Utf8NoBom -Path (Join-Path $localBin "local-worker.ps1") -Content "`$ScriptPath = Join-Path `$PSScriptRoot 'local-model-agent.ps1'`r`n& `$ScriptPath -Mode worker @args`r`n"
    Write-Utf8NoBom -Path (Join-Path $localBin "local-agent.ps1") -Content "`$ScriptPath = Join-Path `$PSScriptRoot 'local-model-agent.ps1'`r`n& `$ScriptPath -Mode agent @args`r`n"
    Write-Utf8NoBom -Path (Join-Path $localBin "gemma-worker.ps1") -Content "`$ScriptPath = Join-Path `$PSScriptRoot 'local-model-agent.ps1'`r`n& `$ScriptPath -Mode worker @args`r`n"
    Write-Utf8NoBom -Path (Join-Path $localBin "gemma-agent.ps1") -Content "`$ScriptPath = Join-Path `$PSScriptRoot 'local-model-agent.ps1'`r`n& `$ScriptPath -Mode agent @args`r`n"
    Write-Log "local-model: installed runtime shims in $localBin"
}

function Sync-AgentUtilities {
    $localBin = Join-Path $HomeDir ".local\bin"
    New-Item -ItemType Directory -Force -Path $localBin | Out-Null

    if (Test-Path -LiteralPath $CanonicalAgentNowScript) {
        $runtimeScript = Join-Path $localBin "agent-now.ps1"
        Set-CanonicalFile -Source $CanonicalAgentNowScript -Target $runtimeScript
        Write-Utf8NoBom -Path (Join-Path $localBin "agent-now.cmd") -Content "@echo off`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File `"%~dp0agent-now.ps1`" %*`r`n"
        Write-Log "utils: installed agent-now in $localBin"
    }
    else {
        Write-Log "utils: missing source $CanonicalAgentNowScript"
    }
}

function Sync-UniversalVaultSkills {
    if (-not (Test-Path -LiteralPath $UniversalSkillSourceRoot)) {
        return
    }
    New-Item -ItemType Directory -Force -Path $AgentSkillRoot | Out-Null
    foreach ($sourceSkill in Get-ChildItem -LiteralPath $UniversalSkillSourceRoot -Directory) {
        $agentSkill = Join-Path $AgentSkillRoot $sourceSkill.Name
        Set-CanonicalDirectory -Source $sourceSkill.FullName -Target $agentSkill

        foreach ($runtimeRoot in @(
            (Join-Path $HomeDir ".claude\skills"),
            (Join-Path $HomeDir ".codex\skills")
        )) {
            New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
            # Link to the HUB copy ($agentSkill), not the vault source directly.
            # skills-sync.py (run right after, in Sync-ManifestSkills) links
            # these same runtime paths to the hub too; pointing here at the
            # vault source instead made every run recreate the link back and
            # forth between two different targets on every single pass.
            Set-CanonicalDirectory -Source $agentSkill -Target (Join-Path $runtimeRoot $sourceSkill.Name)
        }
    }
}

function Set-JsonProperty {
    param(
        [Parameter(Mandatory = $true)][pscustomobject]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)]$Value
    )
    if ($Object.PSObject.Properties.Name -contains $Name) {
        $Object.$Name = $Value
    }
    else {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

function New-McpRemoteServer {
    param(
        [Parameter(Mandatory = $true)][string]$Endpoint,
        [Parameter(Mandatory = $true)][string]$TokenExpression
    )
    return [pscustomobject]@{
        command = "npx"
        args = @(
            "-y",
            "mcp-remote",
            $Endpoint,
            "--header",
            "Authorization: Bearer $TokenExpression"
        )
    }
}

function New-McpStdioServer {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [hashtable]$Environment
    )
    $server = [pscustomobject]@{
        command = $Command
        args = $Arguments
    }
    if ($Environment) {
        $server | Add-Member -NotePropertyName "env" -NotePropertyValue ([pscustomobject]$Environment)
    }
    return $server
}

function Sync-ManifestSkills {
    # Skills from the manifest (including third-party GitHub ones) + INDEX.md catalog.
    # skills-sync.py is idempotent and additive: safe in the recurring lane.
    # Without this call, a skill registered in the manifest only arrives
    # wherever someone runs --apply by hand (the humanizer bug).
    $skillsSync = Join-Path $EngineRoot "scripts\skills-sync.py"
    if (-not (Test-Path -LiteralPath $skillsSync)) {
        Write-Log "skills-manifest: missing script $skillsSync"
        return
    }
    if (-not (Get-Command "python" -ErrorAction SilentlyContinue)) {
        Write-Log "skills-manifest: python not in PATH, skipping"
        return
    }
    $out = & python $skillsSync --apply 2>&1
    if ($LASTEXITCODE -eq 0) {
        $summary = ($out | Select-String -Pattern "Total:" | Select-Object -Last 1)
        Write-Log "skills-manifest: apply ok ($($summary -replace '\e\[[0-9;]*m', ''))"
    }
    else {
        Write-Log "skills-manifest: apply had a FAIL (best-effort, detail in the manual diff)"
    }
}

function Sync-AntigravityMcp {
    if ($SkipMcp) {
        Write-Log "mcp: skipped by parameter"
        return
    }

    $vaultEndpoint = $env:VAULT_LIBRARY_URL
    if (-not $vaultEndpoint -and $env:VAULT_LIBRARY_HOST -and $env:VAULT_LIBRARY_PATH) {
        $hostValue = $env:VAULT_LIBRARY_HOST.TrimEnd("/")
        $pathValue = $env:VAULT_LIBRARY_PATH
        if ($hostValue -notmatch "^https?://") {
            $hostValue = "https://$hostValue"
        }
        if ($pathValue -notmatch "^/") {
            $pathValue = "/$pathValue"
        }
        $vaultEndpoint = "$hostValue$pathValue"
    }
    if (-not $vaultEndpoint) {
        $vaultEndpoint = "https://vault.$env:REMOTE_HOST.sslip.io/mcp-<id>"
    }

    $definitions = [ordered]@{
        "n8n-mcp" = (New-McpRemoteServer -Endpoint "http://127.0.0.1:5678/mcp-server/http" -TokenExpression '${N8N_MCP_TOKEN}')
        "vault-library" = (New-McpRemoteServer -Endpoint $vaultEndpoint -TokenExpression '${VAULT_LIBRARY_TOKEN}')
        "firecrawl" = (New-McpStdioServer -Command "npx" -Arguments @("-y", "firecrawl-mcp") -Environment @{ FIRECRAWL_API_URL = "http://127.0.0.1:33002" })
        "playwright" = (New-McpStdioServer -Command "npx" -Arguments @("-y", "@playwright/mcp@latest", "--cdp-endpoint", "http://localhost:9222"))
        "google-calendar" = (New-McpStdioServer -Command "npx" -Arguments @("-y", "@cocal/google-calendar-mcp") -Environment @{ GOOGLE_OAUTH_CREDENTIALS = (Join-Path $HomeDir ".config\agent-secrets\gcp-oauth.keys.json") })
    }

    foreach ($configPath in @(
        (Join-Path $HomeDir ".gemini\antigravity\mcp_config.json"),
        (Join-Path $HomeDir ".gemini\antigravity-ide\mcp_config.json"),
        (Join-Path $HomeDir ".gemini\config\mcp_config.json")
    )) {
        $configDir = Split-Path -Path $configPath -Parent
        New-Item -ItemType Directory -Force -Path $configDir | Out-Null

        $existingContent = ""
        $hadExistingConfig = Test-Path -LiteralPath $configPath
        if ($hadExistingConfig) {
            $existingContent = Get-Content -LiteralPath $configPath -Raw
            $json = $existingContent | ConvertFrom-Json
        }
        else {
            $json = [pscustomobject]@{}
        }

        if (-not ($json.PSObject.Properties.Name -contains "mcpServers") -or -not $json.mcpServers) {
            $json | Add-Member -NotePropertyName "mcpServers" -NotePropertyValue ([pscustomobject]@{})
        }

        foreach ($serverName in $definitions.Keys) {
            Set-JsonProperty -Object $json.mcpServers -Name $serverName -Value $definitions[$serverName]
        }

        $content = $json | ConvertTo-Json -Depth 40
        $finalContent = $content + "`r`n"
        if ($hadExistingConfig -and $existingContent -eq $finalContent) {
            Write-Log "mcp: already canonical in $configPath"
            continue
        }
        if ($hadExistingConfig) {
            Backup-Path -Path $configPath
        }
        Write-Utf8NoBom -Path $configPath -Content $finalContent
        Write-Log "mcp: ensured canonical Antigravity servers in $configPath"
    }
}

function Sync-OpenCode {
    # OpenCode: self-managed config on Windows too, same as Linux. There is no
    # canonical opencode.json template in the vault to sync from (render.py
    # already covers OpenCode's MCP section directly via `--write opencode`
    # once its Windows dialect lands; until then this is intentionally a
    # no-op, not a dead sync pointed at a file that doesn't exist).
}

function Ensure-AlertCreds {
    if ($Remote -eq "local" -or $Remote -eq "none") { return }
    # Auto-provisioning (2 macchine fidate): se mancano le creds Telegram, le recupera da n8n e le persiste.
    # I segreti non viaggiano nel vault: vengono ridistribuiti on-demand dalla fonte n8n. Best-effort.
    try {
        if ($env:TELEGRAM_BOT_TOKEN -and $env:TELEGRAM_CHAT_ID) { return }
        $u = [Environment]::GetEnvironmentVariable('TELEGRAM_BOT_TOKEN', 'User')
        if ($u) {
            $env:TELEGRAM_BOT_TOKEN = $u
            if (-not $env:TELEGRAM_CHAT_ID) { $env:TELEGRAM_CHAT_ID = [Environment]::GetEnvironmentVariable('TELEGRAM_CHAT_ID', 'User') }
            return
        }
        # Sorgente n8n opt-in via env/template: nessun identificatore personale nel codice.
        # Senza queste variabili il provisioning automatico si salta e restano gli alert via env.
        $credId = $env:N8N_TELEGRAM_CRED_ID
        $chat = $env:TELEGRAM_CHAT_ID
        $container = if ($env:N8N_CONTAINER) { $env:N8N_CONTAINER } else { "n8n-n8n-1" }
        if (-not $credId -or -not $chat -or -not $env:REMOTE_ALIAS) {
            Write-Log "alert-creds: sorgente n8n non configurata (N8N_TELEGRAM_CRED_ID / TELEGRAM_CHAT_ID / REMOTE_ALIAS) - salto, uso alert via env se presenti"
            return
        }
        $remote = @"
n8n export:credentials --all --decrypted --output=/tmp/c.json >/dev/null 2>&1
node -e 'const d=require("/tmp/c.json");const c=d.find(x=>x.id==="$credId");process.stdout.write((c&&c.data&&(c.data.accessToken||c.data.token))||"")' 2>/dev/null
rm -f /tmp/c.json
"@
        # Retry breve: assorbe i transitori SSH/remoto.
        $tok = ""
        for ($i = 1; $i -le 3 -and -not $tok; $i++) {
            $tok = ($remote | & ssh -o ConnectTimeout=12 -o BatchMode=yes $env:REMOTE_ALIAS "sudo -n docker exec -i $container sh -s" 2>$null | Out-String).Trim()
            if (-not $tok -and $i -lt 3) { Start-Sleep -Seconds 4 }
        }
        if (-not $tok) { Write-Log "alert-creds: provisioning Telegram NON riuscito dopo 3 tentativi (remoto irraggiungibile o cred non recuperata) - riprovera' al prossimo agent-sync"; return }
        [Environment]::SetEnvironmentVariable('TELEGRAM_BOT_TOKEN', $tok, 'User')
        [Environment]::SetEnvironmentVariable('TELEGRAM_CHAT_ID', $chat, 'User')
        $env:TELEGRAM_BOT_TOKEN = $tok; $env:TELEGRAM_CHAT_ID = $chat
        Write-Log "alert-creds: provisioning Telegram da n8n completato"
    }
    catch { Write-Log "alert-creds: provisioning non riuscito ($($_.Exception.Message))" }
}

function Send-Healthcheck {
    # Healthcheck raggruppato su Telegram: 1/giorno + escalation immediata su problema NUOVO.
    # Contenuto = riepilogo di agent-doctor.ps1 (drift sync, MCP, istruzioni, token, skill...).
    # Blindata in try/catch: non deve mai far fallire il sync.
    try {
        $doctor = Join-Path $EngineRoot "scripts\agent-doctor.ps1"
        if (-not (Test-Path -LiteralPath $doctor)) { return }
        $doctorTimeout = if ($env:AGENT_DOCTOR_TIMEOUT_SECONDS) { [int]$env:AGENT_DOCTOR_TIMEOUT_SECONDS } else { 20 }
        $job = Start-Job -ScriptBlock {
            param([string]$DoctorPath)
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $DoctorPath -Summary 2>$null | Select-Object -Last 1
        } -ArgumentList $doctor
        try {
            if (Wait-Job -Job $job -Timeout $doctorTimeout) {
                $summary = Receive-Job -Job $job | Select-Object -Last 1
            }
            else {
                Stop-Job -Job $job -ErrorAction SilentlyContinue
                Write-Log "healthcheck: skipped (agent-doctor timeout after ${doctorTimeout}s)"
                return
            }
        }
        finally {
            Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        }
        if (-not $summary) { return }

        $problem = $summary -match 'FAIL=[1-9]'
        $sig = ($summary -replace '\s', '')
        $stateFile = Join-Path $LogDir "agent-healthcheck.state"
        $interval = if ($env:AGENT_HEALTHCHECK_INTERVAL) { [int]$env:AGENT_HEALTHCHECK_INTERVAL } else { 86400 }
        $now = [int][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

        $last = 0; $lastSig = ""
        if (Test-Path -LiteralPath $stateFile) {
            $lines = Get-Content -LiteralPath $stateFile
            if ($lines.Count -ge 1 -and $lines[0] -match '^\d+$') { $last = [int]$lines[0] }
            if ($lines.Count -ge 2) { $lastSig = $lines[1] }
        }

        # Invia SOLO se qualcosa non va (FAIL). Nessun report verde di routine.
        if (-not $problem) {
            Write-Utf8NoBom -Path $stateFile -Content "$now`nok"
            return
        }
        $send = $false
        if ($sig -ne $lastSig) { $send = $true }
        if (($now - $last) -ge $interval) { $send = $true }
        if (-not $send) { return }

        $hostn = $env:COMPUTERNAME
        $msg = "[PROBLEMA AGENTI] [$hostn] " + (Get-Date -Format "yyyy-MM-dd HH:mm") + "`n" + $summary

        $sent = $false
        if ($env:TELEGRAM_BOT_TOKEN -and $env:TELEGRAM_CHAT_ID) {
            try { Invoke-RestMethod -Uri "https://api.telegram.org/bot$($env:TELEGRAM_BOT_TOKEN)/sendMessage" -Method Post -Body @{ chat_id = $env:TELEGRAM_CHAT_ID; text = $msg } -TimeoutSec 10 | Out-Null; $sent = $true } catch {}
        }
        elseif ($env:VAULT_ALERT_WEBHOOK) {
            try { Invoke-RestMethod -Uri $env:VAULT_ALERT_WEBHOOK -Method Post -Body @{ host = $hostn; text = $msg } -TimeoutSec 10 | Out-Null; $sent = $true } catch {}
        }
        if ($sent) { Write-Log "healthcheck: inviato ($sig)" } else { Write-Log "healthcheck: $summary (nessun transport configurato)" }

        Write-Utf8NoBom -Path $stateFile -Content "$now`n$sig"
    }
    catch { Write-Log "healthcheck: non-fatal error ($($_.Exception.Message))" }
}

function Install-StartupFallback {
    param([Parameter(Mandatory = $true)][string]$WrapperPath)
    $startupDir = [Environment]::GetFolderPath("Startup")
    if (-not $startupDir) {
        Write-Log "startup: Startup folder unavailable; logon fallback not installed"
        return
    }
    New-Item -ItemType Directory -Force -Path $startupDir | Out-Null
    $startupVbsPath = Join-Path $startupDir "KnowledgeVault Agent Sync.vbs"
    Copy-Item -LiteralPath $WrapperPath -Destination $startupVbsPath -Force
    Write-Log "startup: installed hidden logon fallback $startupVbsPath"
}

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $oldErrorActionPreference = $ErrorActionPreference
    try {
        $script:ErrorActionPreference = "Continue"
        $output = & $Command @Arguments 2>&1
        return [pscustomobject]@{
            ExitCode = $LASTEXITCODE
            Output = ($output | ForEach-Object { "$_" }) -join "`n"
        }
    }
    finally {
        $script:ErrorActionPreference = $oldErrorActionPreference
    }
}

function Push-LocalCommits {
    if ($Remote -eq "local" -or $Remote -eq "none") {
        Write-Log "push: skipped (Local-Only mode)"
        return
    }
    # The workstations share the same branch on the remote hub. If the other
    # one has published in the meantime, the push gets REJECTED (non-fast-
    # forward): different from "remote hub offline". In that case, clean
    # rebase + retry (benign divergence resolves itself); only a REAL
    # conflict stays manual (rebase --abort, the healthcheck flags it). Never
    # an automatic merge, never lost work.
    try {
        Invoke-GitLogged -Arguments @("fetch", "--prune", $Remote, $Branch)
        $ahead = Get-GitText -Arguments @("rev-list", "--count", "$Remote/$Branch..$Branch")
        if ([int]$ahead -le 0) { return }

        $pushed = $false
        $push = Invoke-GitCapture -Arguments @("push", $Remote, $Branch)
        if ($push.ExitCode -eq 0) {
            $pushed = $true
            Write-Log "push: published $ahead local commit(s) to $Remote"
        }
        else {
            # fetch already ran above -> the remote branch is up to date: the push was rejected, not offline.
            $dirty = Get-GitText -Arguments @("status", "--porcelain", "--untracked-files=no")
            if (-not [string]::IsNullOrWhiteSpace($dirty)) {
                Write-Log "push: rejected but tracked changes uncommitted; not rebasing, resolve manually"
            }
            elseif ((Invoke-GitCapture -Arguments @("rebase", "$Remote/$Branch")).ExitCode -eq 0) {
                if ((Invoke-GitCapture -Arguments @("push", $Remote, $Branch)).ExitCode -eq 0) {
                    $pushed = $true
                    Write-Log "push: divergence resolved via clean rebase, published to $Remote"
                }
                else {
                    Write-Log "push: still rejected after rebase; will retry next run"
                }
            }
            else {
                Invoke-GitCapture -Arguments @("rebase", "--abort") | Out-Null
                Write-Log "push: DIVERGENCE WITH CONFLICTS - manual 'git pull --rebase' needed (healthcheck will flag it)"
            }
        }

        if ($pushed -and $Remote -ne "origin") {
            $originCheck = Invoke-GitCapture -Arguments @("remote", "get-url", "origin")
            if ($originCheck.ExitCode -eq 0) {
                $originPush = Invoke-GitCapture -Arguments @("push", "origin", $Branch)
                if ($originPush.ExitCode -eq 0) { Write-Log "push: mirrored commits to origin" }
                else { Write-Log "push: origin unavailable; Oracle push completed" }
            }
        }
    }
    catch {
        Write-Log "push: skipped or failed ($($_.Exception.Message))"
    }
}

function Ensure-AgentSyncHiddenWrapper {
    param([Parameter(Mandatory = $true)][string]$ScriptPath)
    $wrapperPath = Join-Path $EngineRoot "scripts\start-agent-sync-hidden.vbs"
    $escapedScriptPath = $ScriptPath.Replace('"', '""')
    $content = @"
Set shell = CreateObject("WScript.Shell")
script = "$escapedScriptPath"
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & Chr(34) & script & Chr(34) & " -Mode guard", 0, True
"@
    Write-Utf8NoBom -Path $wrapperPath -Content ($content.TrimEnd() + "`r`n")
    return $wrapperPath
}

function Install-AgentSyncTask {
    $scriptPath = $PSCommandPath
    if (-not $scriptPath) {
        $scriptPath = Join-Path $EngineRoot "scripts\agent-sync.ps1"
    }

    $wrapperPath = Ensure-AgentSyncHiddenWrapper -ScriptPath $scriptPath
    $argument = "`"$wrapperPath`""
    try {
        $action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument $argument
        $logonTrigger = New-ScheduledTaskTrigger -AtLogOn
        $repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Days 3650)
        $userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
        $settings.Hidden = $true
        $task = New-ScheduledTask -Action $action -Trigger @($logonTrigger, $repeatTrigger) -Principal $principal -Settings $settings -Description "Cloud-first KnowledgeVault guard. Pull best-effort, reassert runtime files, run healthcheck. No publish."
        Register-ScheduledTask -TaskName $ScheduledTaskName -InputObject $task -Force | Out-Null
        Write-Log "scheduled-task: installed/updated '$ScheduledTaskName' via Register-ScheduledTask"
        return
    }
    catch {
        Write-Log "scheduled-task: Register-ScheduledTask failed; falling back to schtasks.exe ($($_.Exception.Message))"
    }

    $taskRun = "wscript.exe $argument"
    $everyThirtyArgs = @("/Create", "/TN", $ScheduledTaskName, "/SC", "MINUTE", "/MO", "30", "/TR", $taskRun, "/F")
    $logonArgs = @("/Create", "/TN", "$ScheduledTaskName Logon", "/SC", "ONLOGON", "/TR", $taskRun, "/F")

    $result = Invoke-NativeCapture -Command "schtasks.exe" -Arguments $everyThirtyArgs
    if ($result.ExitCode -ne 0) {
        throw "schtasks.exe failed for '$ScheduledTaskName': $($result.Output)"
    }
    Write-Log "scheduled-task: installed/updated '$ScheduledTaskName' via schtasks.exe"

    $result = Invoke-NativeCapture -Command "schtasks.exe" -Arguments $logonArgs
    if ($result.ExitCode -ne 0) {
        Write-Log "scheduled-task: logon trigger failed via schtasks.exe; installing Startup fallback ($($result.Output))"
        Install-StartupFallback -WrapperPath $wrapperPath
        return
    }
    Write-Log "scheduled-task: installed/updated '$ScheduledTaskName Logon' via schtasks.exe"
}

function Sync-ClaudeHooks {
    # Deploy the canonical vault hook into the Claude runtime and merge its triggers into
    # settings.json. Universal: same hook on every machine. Idempotent. Preserves other hooks.
    $hookSource = Join-Path $LayerDir "hooks\claude-vault-checkpoint.mjs"
    if (-not (Test-Path -LiteralPath $hookSource)) {
        Write-Log "claude-hooks: missing source $hookSource"
        return
    }
    $claudeDir = Join-Path $HomeDir ".claude"
    if (-not (Test-Path -LiteralPath $claudeDir)) {
        Write-Log "claude-hooks: no Claude runtime at $claudeDir; skipping"
        return
    }
    $hookTarget = Join-Path $claudeDir "claude-vault-checkpoint.mjs"
    Set-CanonicalFile -Source $hookSource -Target $hookTarget

    $settingsPath = Join-Path $claudeDir "settings.json"
    if (-not (Test-Path -LiteralPath $settingsPath)) {
        Write-Log "claude-hooks: settings.json not found; skipping merge"
        return
    }
    $command = "node `"$hookTarget`""

    try {
        $json = (Get-Content -LiteralPath $settingsPath -Raw) | ConvertFrom-Json
    }
    catch {
        Write-Log "claude-hooks: settings.json not valid JSON; skipping merge ($($_.Exception.Message))"
        return
    }

    if (-not ($json.PSObject.Properties.Name -contains "hooks") -or -not $json.hooks) {
        $json | Add-Member -NotePropertyName "hooks" -NotePropertyValue ([pscustomobject]@{}) -Force
    }

    $changed = $false
    foreach ($evt in @("SessionStart", "PreCompact")) {
        if (-not ($json.hooks.PSObject.Properties.Name -contains $evt) -or -not $json.hooks.$evt) {
            $json.hooks | Add-Member -NotePropertyName $evt -NotePropertyValue @() -Force
        }
        $existing = @($json.hooks.$evt)
        $present = $false
        foreach ($matcher in $existing) {
            foreach ($h in @($matcher.hooks)) {
                if ($h.command -eq $command) { $present = $true }
            }
        }
        if (-not $present) {
            $entry = [pscustomobject]@{
                hooks = @([pscustomobject]@{ type = "command"; command = $command; timeout = 5 })
            }
            $json.hooks.$evt = @($existing + $entry)
            $changed = $true
        }
    }

    if (-not $changed) {
        Write-Log "claude-hooks: already present in $settingsPath"
        return
    }
    Backup-Path -Path $settingsPath
    $content = ($json | ConvertTo-Json -Depth 40) + "`r`n"
    Write-Utf8NoBom -Path $settingsPath -Content $content
    Write-Log "claude-hooks: merged SessionStart/PreCompact into $settingsPath"
}

Write-Log "agent-sync: start mode=$Mode"
if ($DoPull) {
    Invoke-BestEffortPull
}
if ($DoApply) {
    Sync-Instructions
    Sync-LocalModelRuntime
    Sync-AgentUtilities
    Sync-UniversalVaultSkills
    Sync-ManifestSkills
    Sync-AntigravityMcp
    Sync-OpenCode
    Sync-ClaudeHooks
}
if ($DoPush) {
    Push-LocalCommits
}
if ($DoCreds) {
    Ensure-AlertCreds
}
if ($DoHealth) {
    Send-Healthcheck
}
if ($InstallScheduledTask) {
    Install-AgentSyncTask
}

$dirty = ""
try {
    $dirty = Get-GitText -Arguments @("status", "--porcelain")
}
catch {
    $dirty = ""
}
if ($dirty) {
    $dirtyCount = ($dirty -split "`n" | Where-Object { $_.Trim() }).Count
    Write-Log "note: $dirtyCount uncommitted vault file(s); not auto-committing"
}

Write-Log "agent-sync: completed"
