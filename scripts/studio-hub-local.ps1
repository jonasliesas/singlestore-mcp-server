# Run the SAS SpeedyStore Studio hub (server pilot) on this PC, signing in with SAS Logon on your Viya.
#
#   .\scripts\studio-hub-local.ps1                       # Viya + SingleStore host from your own saved connections
#   .\scripts\studio-hub-local.ps1 -ViyaUrl https://zeus.example.sas.com -Port 8800
#   .\scripts\studio-hub-local.ps1 -Check                # only check prerequisites and print the checklist
#   .\scripts\studio-hub-local.ps1 -Notebooks -NotebookUsers "norjni,<colleague>"   # with local notebooks
#   .\scripts\studio-hub-local.ps1 -UsersGroup SpeedyStoreStudioUsers -NotebookGroup SpeedyStoreStudioNotebooks
#   .\scripts\studio-hub-local.ps1 -Gateway -GatewayVenv C:\path\to\eg-venv -NotebookUsers "norjni"   # kernel gateway (2a)
#
# -Notebooks: STUDIO_NOTEBOOKS=local with your desktop notebook environment (~\.singlestore-mcp\notebook-env). Without
# groups only -NotebookUsers (default: -Admins) may then use Studio and notebooks. Local notebooks have no isolation
# between users' kernels: test use with a few trusted users only (the kernel gateway, step 2a, replaces this).
# -Gateway: STUDIO_NOTEBOOKS=gateway - a local Jupyter Enterprise Gateway (python -m singlestore_mcp.kernel_gateway) from
# -GatewayVenv (a separate venv: uv venv eg --python 3.11; uv pip install --python eg -r deploy\gateway-constraints.txt)
# on -GatewayPort, kernels in <DataDir>\kernels, a random gateway token for this run, package installs -PipMode.
#
# Then open http://127.0.0.1:<port>/ and sign in. Without an own OAuth client in SAS Logon this uses the "paste the
# code" sign-in with Viya's built-in vscode client. Your password is only typed into SAS Logon.
# Users' folders go to ~\.studio-hub-pilot (not your normal ~\.singlestore-mcp). Ctrl+C stops the hub and every Studio.
# Works from the main checkout and from a git worktree (the worktree's code, the main checkout's .venv).
param(
    [string]$ViyaUrl = "",
    [int]$Port = 8800,
    [string]$DataDir = "$HOME\.studio-hub-pilot",
    [string]$Admins = $env:USERNAME,
    [int]$IdleMinutes = 30,
    [switch]$Notebooks,
    [string]$NotebookUsers = "",
    [string]$UsersGroup = "",
    [string]$NotebookGroup = "",
    [switch]$Gateway,
    [string]$GatewayVenv = $env:STUDIO_GATEWAY_VENV,
    [int]$GatewayPort = 8888,
    [ValidateSet("off", "pypi", "mirror")][string]$PipMode = "off",
    [switch]$Check
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$home2 = if ($env:SINGLESTORE_MCP_HOME) { $env:SINGLESTORE_MCP_HOME } else { "$HOME\.singlestore-mcp" }
$problems = New-Object System.Collections.Generic.List[string]

function Fail([string]$what, [string]$fix) { $problems.Add("$what`n      -> $fix") }

# ---------------------------------------------------------------- prerequisites
# Python: this checkout's .venv, else (in a git worktree) the main checkout's .venv.
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $main = $null
    try { $common = (& git -C $repo rev-parse --path-format=absolute --git-common-dir 2>$null); if ($common) { $main = Split-Path -Parent $common } } catch { }
    if ($main -and (Test-Path (Join-Path $main ".venv\Scripts\python.exe"))) {
        $python = Join-Path $main ".venv\Scripts\python.exe"
    } else {
        Fail "No Python environment: $repo\.venv\Scripts\python.exe is missing." "Run 'uv sync' in $repo (or in the main checkout when this is a worktree)."
        $python = $null
    }
}
if ($python) {
    $env:PYTHONPATH = Join-Path $repo "src"   # this checkout's code, even with another checkout's .venv
    & $python -c "import singlestore_mcp.hub.app" 2>$null
    if ($LASTEXITCODE -ne 0) { Fail "The hub doesn't import with $python." "Run 'uv sync' (the environment may be missing packages), then try again." }
}

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
if (-not $ViyaUrl) { Fail "No SAS Viya address." "Give -ViyaUrl https://<your viya> (or save a SAS Viya connection in Studio first)." }
elseif ($ViyaUrl -notmatch '^https?://') { $ViyaUrl = "https://$ViyaUrl" }

if ($Port -lt 1 -or $Port -gt 65535) { Fail "Port $Port isn't a port number." "Use e.g. -Port 8800." }
else {
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.ExclusiveAddressUse = $true
        $listener.Start()
    } catch {
        $owner = ""
        try {
            $procId = (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1).OwningProcess
            $owner = " (used by $((Get-Process -Id $procId).ProcessName), PID $procId)"
        } catch { }
        Fail "Port $Port is already in use$owner." "Stop that program (an older hub?), or start with another port: -Port 8801."
    } finally { if ($listener) { $listener.Stop() } }
}

$nbPython = $null
if ($Gateway) { $Notebooks = $true }
$egExe = $null
if ($Gateway) {
    $egExe = if ($GatewayVenv) { Join-Path $GatewayVenv "Scripts\jupyter-enterprisegateway.exe" } else { "" }
    if (-not $GatewayVenv -or -not (Test-Path $egExe)) {
        Fail "-Gateway: no Enterprise Gateway venv (-GatewayVenv '$GatewayVenv')." "uv venv <folder> --python 3.11; uv pip install --python <folder> -r deploy\gateway-constraints.txt; then -GatewayVenv <folder>."
    }
    if ($GatewayPort -eq $Port -or (8851..8853) -contains $GatewayPort) { Fail "-GatewayPort $GatewayPort clashes." "Use another port, e.g. -GatewayPort 8888." }
    if ($PipMode -eq "mirror" -and -not $env:STUDIO_PIP_INDEX_URL) { Fail "-PipMode mirror needs STUDIO_PIP_INDEX_URL." "Set it to the mirror's /simple index." }
}
if ($Notebooks) {
    $nbPython = if ($env:STUDIO_NOTEBOOK_PYTHON) { $env:STUDIO_NOTEBOOK_PYTHON } else { Join-Path $home2 "notebook-env\Scripts\python.exe" }
    if (-not (Test-Path $nbPython)) {
        Fail "-Notebooks: no notebook environment at $nbPython." "Open a notebook once in your desktop Workspace (it installs the environment), or set STUDIO_NOTEBOOK_PYTHON."
    }
}

try { New-Item -ItemType Directory -Force -Path $DataDir | Out-Null } catch { Fail "Can't create the data folder $DataDir." "Give another folder with -DataDir." }

if ($problems.Count) {
    Write-Host ""
    Write-Host "Studio hub can't start:" -ForegroundColor Red
    foreach ($p in $problems) { Write-Host "  - $p" }
    Write-Host ""
    exit 1
}

# ---------------------------------------------------------------- settings
# The active SingleStore connection's host/port/database as everyone's default (no user, no password).
$s2 = "none (each user adds one in Connections)"
$conn = Join-Path $home2 "connections.json"
if (Test-Path $conn) {
    $j = Get-Content $conn -Raw | ConvertFrom-Json
    $p = @($j.profiles) | Where-Object { $_.name -eq $j.active } | Select-Object -First 1
    if ($p -and $p.host) {
        $env:STUDIO_DEFAULT_S2_NAME = "SingleStore"
        $env:STUDIO_DEFAULT_S2_HOST = $p.host
        $env:STUDIO_DEFAULT_S2_PORT = "$($p.port)"
        if ($p.database) { $env:STUDIO_DEFAULT_S2_DATABASE = $p.database }
        $s2 = "$($p.host):$($p.port)$(if ($p.database) { "/$($p.database)" }) (user = the SAS user id, no password)"
    }
}

$env:STUDIO_VIYA_URL = $ViyaUrl
$env:STUDIO_HUB_PORT = "$Port"
if (-not $env:STUDIO_PUBLIC_URL) { $env:STUDIO_PUBLIC_URL = "http://127.0.0.1:$Port" }
$env:STUDIO_DATA_DIR = $DataDir
$env:STUDIO_ADMINS = $Admins
$env:STUDIO_IDLE_MINUTES = "$IdleMinutes"
if (-not $env:STUDIO_OAUTH_CLIENT_ID) { $env:STUDIO_OAUTH_MODE = "paste" }
if ($UsersGroup) { $env:STUDIO_USERS_GROUP = $UsersGroup }
if ($NotebookGroup) { $env:STUDIO_NOTEBOOK_GROUP = $NotebookGroup }
$nbText = "off"
if ($Notebooks) {
    $env:STUDIO_NOTEBOOKS = if ($Gateway) { "gateway" } else { "local" }
    $env:STUDIO_NOTEBOOK_PYTHON = $nbPython
    if (-not $NotebookGroup) {
        if (-not $NotebookUsers) { $NotebookUsers = $Admins }
        $env:STUDIO_ALLOWED_USERS = $NotebookUsers
        $nbText = "$($env:STUDIO_NOTEBOOKS), for $NotebookUsers"
    } else { $nbText = "$($env:STUDIO_NOTEBOOKS), for members of $NotebookGroup" }
}
if ($Gateway) {
    $env:STUDIO_GATEWAY_URL = "http://127.0.0.1:$GatewayPort"
    $env:STUDIO_GATEWAY_AUTH_TOKEN = [Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(36))
    $env:STUDIO_PIP_MODE = $PipMode
    if (-not $env:STUDIO_RELAY_PORT) { $env:STUDIO_RELAY_PORT = "8801" }
    $nbText += " - gateway on 127.0.0.1:$GatewayPort, kernels in $DataDir\kernels, package installs: $PipMode"
}
$access = if ($UsersGroup -or $NotebookGroup) { "SAS groups: $(@($UsersGroup, $NotebookGroup) -ne '' -join ', ')" + $(if ($env:STUDIO_ALLOWED_USERS) { " + $($env:STUDIO_ALLOWED_USERS)" } else { "" }) }
          elseif ($env:STUDIO_ALLOWED_USERS) { "only $($env:STUDIO_ALLOWED_USERS)" } else { "everyone who can sign in to SAS Logon" }
$flow = if ($env:STUDIO_OAUTH_MODE -eq "paste" -or -not $env:STUDIO_OAUTH_CLIENT_ID) {
    "paste the code - SAS Logon (Viya's built-in 'vscode' client) shows a code after you sign in; paste it into the hub's page"
} else {
    "redirect - the hub's own SAS Logon client '$($env:STUDIO_OAUTH_CLIENT_ID)' (redirect URI $($env:STUDIO_PUBLIC_URL)/oauth/callback)"
}
$key = if ($env:STUDIO_MASTER_KEY) { "from STUDIO_MASTER_KEY" } else { "key file $DataDir\hub\master.key (made once; fine on a PC)" }

Write-Host ""
Write-Host "SAS SpeedyStore Studio hub (server pilot)" -ForegroundColor Cyan
Write-Host "  Open       $($env:STUDIO_PUBLIC_URL)/   (admin page: $($env:STUDIO_PUBLIC_URL)/admin)"
Write-Host "  Sign-in    $flow"
Write-Host "  SAS Logon  $ViyaUrl   (your password is typed there only, never into the hub)"
Write-Host "  Admins     $Admins"
Write-Host "  Access     $access"
Write-Host "  Notebooks  $nbText"
if ($Notebooks) { Write-Host "             no isolation between users' kernels - test use with a few trusted users only" -ForegroundColor Yellow }
Write-Host "  SingleStore default  $s2"
Write-Host "  Idle stop  $IdleMinutes min    Secrets key: $key"
Write-Host "  Logs       hub + audit: this window and $DataDir\hub\audit.jsonl"
Write-Host "             each user's Studio: $DataDir\users\<sas-user>\state\studio.log"
Write-Host "  Python     $python   (code: $repo\src)"
Write-Host "  Stop       Ctrl+C (stops the hub and every Studio)"
Write-Host ""
if ($Check) { Write-Host "Prerequisites OK (-Check: not starting)." -ForegroundColor Green; exit 0 }

$gw = $null
if ($Gateway) {
    # The gateway gets its token, limits and folders from these (its own process; the token is for this run only).
    $env:STUDIO_GATEWAY_EG = $egExe
    $env:STUDIO_GATEWAY_BIND = "127.0.0.1"
    $env:STUDIO_GATEWAY_PORT = "$GatewayPort"
    $env:STUDIO_KERNEL_DATA_DIR = Join-Path $DataDir "kernels"
    $env:STUDIO_GATEWAY_RUNTIME = Join-Path $DataDir "gateway-runtime"
    $gw = Start-Process -FilePath $python -ArgumentList "-m", "singlestore_mcp.kernel_gateway" -PassThru -NoNewWindow `
        -RedirectStandardError (Join-Path $DataDir "hub\gateway.log") -RedirectStandardOutput (Join-Path $DataDir "hub\gateway.out")
    Write-Host "Kernel gateway started (PID $($gw.Id), log $DataDir\hub\gateway.log)."
}
try {
    & $python -m singlestore_mcp.hub
} finally {
    if ($gw) { & taskkill /T /F /PID $gw.Id 2>$null | Out-Null }
}
