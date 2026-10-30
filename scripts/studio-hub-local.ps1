# Run the SAS SpeedyStore Studio hub (server pilot) on this PC, signing in with SAS Logon on your Viya.
#
#   .\scripts\studio-hub-local.ps1                       # Viya + SingleStore host from your own saved connections
#   .\scripts\studio-hub-local.ps1 -ViyaUrl https://zeus.example.sas.com -Port 8800
#
# Then open http://127.0.0.1:<port>/ and sign in. Without an own OAuth client in SAS Logon this uses the "paste the
# code" sign-in with Viya's built-in vscode client. Your password is only typed into SAS Logon.
# Users' folders go to ~\.studio-hub-pilot (not your normal ~\.singlestore-mcp). Ctrl+C stops the hub and every Studio.
param(
    [string]$ViyaUrl = "",
    [int]$Port = 8800,
    [string]$DataDir = "$HOME\.studio-hub-pilot",
    [string]$Admins = $env:USERNAME,
    [int]$IdleMinutes = 30
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$home2 = if ($env:SINGLESTORE_MCP_HOME) { $env:SINGLESTORE_MCP_HOME } else { "$HOME\.singlestore-mcp" }

if (-not $ViyaUrl) {
    # The active SAS Viya connection's address (sas.json holds no secrets).
    $sas = Join-Path $home2 "sas.json"
    if (Test-Path $sas) {
        $j = Get-Content $sas -Raw | ConvertFrom-Json
        $c = @($j.connections) | Where-Object { $_.name -eq $j.active } | Select-Object -First 1
        if (-not $c) { $c = @($j.connections)[0] }
        if ($c) { $ViyaUrl = $c.url }
    }
}
if (-not $ViyaUrl) { throw "Give -ViyaUrl https://<your viya>" }

# The active SingleStore connection's host/port/database as everyone's default (no user, no password).
$conn = Join-Path $home2 "connections.json"
if (Test-Path $conn) {
    $j = Get-Content $conn -Raw | ConvertFrom-Json
    $p = @($j.profiles) | Where-Object { $_.name -eq $j.active } | Select-Object -First 1
    if ($p -and $p.host) {
        $env:STUDIO_DEFAULT_S2_NAME = "SingleStore"
        $env:STUDIO_DEFAULT_S2_HOST = $p.host
        $env:STUDIO_DEFAULT_S2_PORT = "$($p.port)"
        if ($p.database) { $env:STUDIO_DEFAULT_S2_DATABASE = $p.database }
    }
}

$env:STUDIO_VIYA_URL = $ViyaUrl
$env:STUDIO_HUB_PORT = "$Port"
$env:STUDIO_PUBLIC_URL = "http://127.0.0.1:$Port"
$env:STUDIO_DATA_DIR = $DataDir
$env:STUDIO_ADMINS = $Admins
$env:STUDIO_IDLE_MINUTES = "$IdleMinutes"
if (-not $env:STUDIO_OAUTH_CLIENT_ID) { $env:STUDIO_OAUTH_MODE = "paste" }
Write-Host "Studio hub: http://127.0.0.1:$Port/  (SAS Logon: $ViyaUrl, users in $DataDir)"
& "$repo\.venv\Scripts\python.exe" -m singlestore_mcp.hub
