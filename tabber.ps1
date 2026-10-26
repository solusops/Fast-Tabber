param(
    [ValidateSet('setup', 'serve', 'check', 'local', 'codex')]
    [string]$Mode = 'check',
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$AgentArgs
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$TabberPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $TabberPython)) { throw 'Create .venv and install requirements.lock.txt first.' }
if ($Mode -eq 'setup') {
    & $TabberPython -m fast_tabber.setup
    exit $LASTEXITCODE
}
if ($Mode -eq 'check') {
    & $TabberPython -m fast_tabber.preflight
    exit $LASTEXITCODE
}
if ($Mode -eq 'serve') {
    & $TabberPython -m fast_tabber.server
    exit $LASTEXITCODE
}
$TabberConnection = Get-Content -LiteralPath '.state\connection.json' -Raw | ConvertFrom-Json
$env:FAST_TABBER_TOKEN = $TabberConnection.token
if ($Mode -eq 'local') {
    & $TabberPython -m fast_tabber.setup
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $env:OPENCODE_CONFIG = Join-Path $PSScriptRoot '.state\opencode.json'
    $env:XDG_CONFIG_HOME = Join-Path $PSScriptRoot '.state\agent-config'
    $env:OPENCODE_DISABLE_AUTOUPDATE = 'true'
    $env:OPENCODE_DISABLE_MODELS_FETCH = 'true'
    $env:OPENCODE_DISABLE_SHARE = 'true'
    $env:OPENCODE_DISABLE_EXTERNAL_SKILLS = 'true'
    $env:OPENCODE_DISABLE_CLAUDE_CODE = 'true'
    $env:OPENCODE_DISABLE_DEFAULT_PLUGINS = 'true'
    $env:OPENCODE_DISABLE_LSP_DOWNLOAD = 'true'
    $env:DO_NOT_TRACK = '1'
    $env:OTEL_SDK_DISABLED = 'true'
    & opencode.cmd --pure @AgentArgs
} else {
    & codex @AgentArgs
}
exit $LASTEXITCODE
