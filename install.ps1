param([string]$Workspace = (Get-Location).Path, [switch]$StartOnly)
$ErrorActionPreference = 'Stop'
$TabberSource = $PSScriptRoot
$Workspace = [IO.Path]::GetFullPath($Workspace)
$TabberRegistry = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.fast-tabber\runtime.json'
$TabberRoot = if (Test-Path -LiteralPath $TabberRegistry) {
    (Get-Content -LiteralPath $TabberRegistry -Raw | ConvertFrom-Json).root
} else { Join-Path ([Environment]::GetFolderPath('UserProfile')) '.fast-tabber\runtime' }
$TabberRoot = [IO.Path]::GetFullPath($TabberRoot)
if ($StartOnly -and !(Test-Path -LiteralPath (Join-Path $TabberRoot '.state\connection.json'))) { throw 'Run install.ps1 once before loading the plugin.' }

function Invoke-TabberCommand([string]$Executable, [string[]]$CommandArgs) {
    & $Executable @CommandArgs
    if ($LASTEXITCODE -ne 0) { throw "$Executable failed with exit code $LASTEXITCODE" }
}
function Get-TabberHealth {
    $TabberConnectionFile = Join-Path $TabberRoot '.state\connection.json'
    if (!(Test-Path -LiteralPath $TabberConnectionFile)) { return $null }
    $TabberConnection = Get-Content -LiteralPath $TabberConnectionFile -Raw | ConvertFrom-Json
    try {
        $TabberHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -Headers @{Authorization="Bearer $($TabberConnection.token)"} -TimeoutSec 3
        if ($TabberHealth.name -ne 'fast-tabber' -or [IO.Path]::GetFullPath($TabberHealth.root) -ne $TabberRoot) { throw 'The local service belongs to a different toolkit installation.' }
        return $TabberHealth
    } catch {
        if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) { throw 'Port 8765 has an unverified or incompatible service; stop it before installation.' }
        return $null
    }
}

Get-Command uv -ErrorAction Stop | Out-Null
$TabberOllamaCommand = Get-Command ollama -ErrorAction SilentlyContinue
$TabberOllama = if ($TabberOllamaCommand) { $TabberOllamaCommand.Source } else { Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\Ollama\ollama.exe' }
if (!(Test-Path -LiteralPath $TabberOllama)) { throw 'Install Ollama, then restart OpenCode or add ollama.exe to PATH.' }
$TabberHealth = Get-TabberHealth
if (!$StartOnly) {
    if ($TabberHealth) {
        if ($TabberHealth.busy) { throw 'Fast-Tabber is busy; finish the current browser operation, then run installation again.' }
    }
    New-Item -ItemType Directory -Path $TabberRoot -Force | Out-Null
    if ([IO.Path]::GetFullPath($TabberSource) -ne $TabberRoot) {
        foreach ($TabberDirectory in @('fast_tabber','extension','.agents\skills\fast-tabber')) {
            New-Item -ItemType Directory -Path (Join-Path $TabberRoot $TabberDirectory) -Force | Out-Null
        }
        Get-ChildItem -LiteralPath (Join-Path $TabberSource 'fast_tabber') -Filter '*.py' | Copy-Item -Destination (Join-Path $TabberRoot 'fast_tabber') -Force
        foreach ($TabberFile in @('extension\manifest.json','extension\background.js','.agents\skills\fast-tabber\SKILL.md','requirements.lock.txt','pyproject.toml','package.json','tabber.ps1','opencode-plugin.js','install.ps1','install.cmd','README.md','LICENSE')) {
            Copy-Item -LiteralPath (Join-Path $TabberSource $TabberFile) -Destination (Join-Path $TabberRoot $TabberFile) -Force
        }
    }
}
Set-Location -LiteralPath $TabberRoot
$TabberPython = Join-Path $TabberRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $TabberPython)) { Invoke-TabberCommand 'uv' @('venv','--python','3.13',(Join-Path $TabberRoot '.venv')) }
if (!$StartOnly) {
    Invoke-TabberCommand 'uv' @('pip','install','--python',$TabberPython,'-r',(Join-Path $TabberRoot 'requirements.lock.txt'))
    Invoke-TabberCommand $TabberPython @('-m','fast_tabber.setup')
}
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_NO_CLOUD = '1'
$env:OLLAMA_CONTEXT_LENGTH = '32768'
try { $TabberModels = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 } catch {
    Start-Process -FilePath $TabberOllama -ArgumentList 'serve' -WorkingDirectory $TabberRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $TabberRoot '.state\ollama.stdout.log') -RedirectStandardError (Join-Path $TabberRoot '.state\ollama.stderr.log') | Out-Null
    for ($TabberWait=0; $TabberWait -lt 30; $TabberWait++) {
        Start-Sleep -Seconds 1
        try { $TabberModels = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2; break } catch {}
    }
    if (!$TabberModels) { throw 'Ollama did not start on loopback.' }
}
if (Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue | Where-Object LocalAddress -NotIn @('127.0.0.1','::1')) { throw 'Ollama must bind only to loopback; configure OLLAMA_HOST=127.0.0.1:11434 and restart it.' }
if (!$StartOnly) {
    $TabberHealth = Get-TabberHealth
    if ($TabberHealth) {
        if ($TabberHealth.busy) { throw 'Fast-Tabber became busy; finish its operation and retry installation.' }
        $TabberConnection = Get-Content -LiteralPath (Join-Path $TabberRoot '.state\connection.json') -Raw | ConvertFrom-Json
        Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/shutdown' -Headers @{Authorization="Bearer $($TabberConnection.token)"} -TimeoutSec 5 | Out-Null
        for ($TabberWait=0; $TabberWait -lt 20 -and (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue); $TabberWait++) { Start-Sleep -Milliseconds 500 }
        if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) { throw 'The previous service did not stop cleanly.' }
    }
}
$TabberHealth = Get-TabberHealth
if (!$TabberHealth) {
    Start-Process -FilePath $TabberPython -ArgumentList '-m','fast_tabber.server' -WorkingDirectory $TabberRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $TabberRoot '.state\server.stdout.log') -RedirectStandardError (Join-Path $TabberRoot '.state\server.stderr.log') | Out-Null
    for ($TabberWait=0; $TabberWait -lt 30; $TabberWait++) {
        Start-Sleep -Seconds 1
        $TabberHealth = Get-TabberHealth
        if ($TabberHealth) { break }
    }
    if (!$TabberHealth) { throw 'MCP service did not start; inspect .state/server.stderr.log.' }
}
if (!$StartOnly) {
    $TabberPlugins = Join-Path $Workspace '.opencode\plugins'
    New-Item -ItemType Directory -Path $TabberPlugins -Force | Out-Null
    $TabberEntry = Join-Path $TabberPlugins 'fast-tabber.js'
    if ((Test-Path -LiteralPath $TabberEntry) -and !(Get-Content -LiteralPath $TabberEntry -Raw).StartsWith('// Fast-Tabber managed entry')) { throw 'A custom fast-tabber.js already exists; preserve or rename it before installing.' }
    $TabberModule = ([Uri](Join-Path $TabberRoot 'opencode-plugin.js')).AbsoluteUri | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($TabberEntry, "// Fast-Tabber managed entry`nexport { FastTabberPlugin } from $TabberModule;`n", (New-Object Text.UTF8Encoding($false)))
    Write-Output "Installed MCP server + Skill. Restart OpenCode in: $Workspace"
    Write-Output 'Use /tabs list my tabs, or select Tabber. Selecting the Skill alone does not install MCP or switch agents.'
    Write-Output 'No models were downloaded. In agent chat ask Fast-Tabber to onboard: select your local model, approve each missing download separately, verify models, then confirm Chrome pairing.'
    Write-Output "Existing Chrome pairing reused when available. First installation only: load $TabberRoot\extension in Chrome and approve its permissions."
}
$TabberExpectedVersion = (Get-Content -LiteralPath (Join-Path $TabberRoot 'extension\manifest.json') -Raw | ConvertFrom-Json).version
if ($TabberHealth.connected -and $TabberHealth.extensionVersion -ne $TabberExpectedVersion) {
    $TabberConnection = Get-Content -LiteralPath (Join-Path $TabberRoot '.state\connection.json') -Raw | ConvertFrom-Json
    Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/reload-extension' -Headers @{Authorization="Bearer $($TabberConnection.token)"} -TimeoutSec 5 | Out-Null
    for ($TabberWait=0; $TabberWait -lt 40; $TabberWait++) {
        Start-Sleep -Seconds 1
        $TabberHealth = Get-TabberHealth
        if ($TabberHealth.connected -and $TabberHealth.extensionVersion -eq $TabberExpectedVersion) { break }
    }
    if (!$TabberHealth.connected -or $TabberHealth.extensionVersion -ne $TabberExpectedVersion) { throw "Chrome needs extension reload/permission approval at $TabberRoot\extension" }
}
Write-Output "Fast-Tabber service ready; Chrome connected: $($TabberHealth.connected)"
