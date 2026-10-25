# Run once from an elevated PowerShell; rules affect only Ollama and OpenCode.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$TabberIdentity = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (!$TabberIdentity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Offline verification needs Administrator rights to temporarily block Ollama/OpenCode external network access. Normal development does not.'
}
$TabberPrograms = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
    (Join-Path $env:APPDATA 'npm\node_modules\opencode-ai\bin\opencode.exe')
)
foreach ($TabberProgram in $TabberPrograms) {
    if (!(Test-Path -LiteralPath $TabberProgram)) { throw "Required executable missing: $TabberProgram" }
}
$TabberRules = @()
$TabberPrefix = 'FastTabberOffline-' + [Guid]::NewGuid().ToString('N')
try {
    if ((Get-NetFirewallProfile | Where-Object Enabled -EQ $false).Count) {
        throw 'A firewall profile is disabled. Cannot claim blocked-network verification.'
    }
    foreach ($TabberProgram in $TabberPrograms) {
        $TabberRules += New-NetFirewallRule -DisplayName $TabberPrefix -Direction Outbound -Action Block `
            -Program $TabberProgram -Profile Any -Enabled True `
            -RemoteAddress @('0.0.0.0-126.255.255.255', '128.0.0.0-255.255.255.255', '::2-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff')
    }
    & .venv\Scripts\python.exe -m fast_tabber.preflight
    if ($LASTEXITCODE -ne 0) { throw 'Model execution failed with external network access blocked.' }
    $TabberSmoke = & powershell -NoProfile -ExecutionPolicy Bypass -File .\tabber.ps1 local run --format json `
        'Call fast-tabber browser_status exactly once. Report only connected status and window/tab counts. Do not read files or run shell commands.'
    if ($LASTEXITCODE -ne 0) { throw 'OpenCode failed with external network access blocked.' }
    $TabberEvents = @($TabberSmoke | ForEach-Object { $_ | ConvertFrom-Json })
    $TabberCalls = @($TabberEvents | Where-Object { $_.type -eq 'tool_use' -and $_.part.tool -eq 'fast-tabber_browser_status' })
    if ($TabberCalls.Count -ne 1 -or $TabberCalls[0].part.state.status -ne 'completed') {
        throw 'OpenCode did not complete exactly one real MCP status call.'
    }
    $TabberStatus = $TabberCalls[0].part.state.output | ConvertFrom-Json
    if ($TabberStatus.connected -ne $true) { throw 'Chrome was not connected during offline verification.' }
    $TabberReport = Get-Content -LiteralPath .state\models.json -Raw | ConvertFrom-Json
    $TabberReport.external_inference_block_test = 'passed: temporary Windows Firewall egress blocks on Ollama and OpenCode; loopback remained available'
    $TabberReport | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath .state\models.json -Encoding utf8
    $TabberSmoke | Set-Content -LiteralPath .state\offline-agent.jsonl -Encoding utf8
    Write-Output 'PASS: Gemma text/tools/vision, embeddings, and OpenCode -> MCP -> Chrome with external egress blocked.'
} finally {
    foreach ($TabberRule in $TabberRules) { Remove-NetFirewallRule -Name $TabberRule.Name }
}
