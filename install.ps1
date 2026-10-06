<#
.SYNOPSIS
    Install or update the SingleStore Workspace on Windows: one command, no Claude needed.

.DESCRIPTION
    Installs uv (which brings Python 3.12), downloads this project, installs its
    dependencies, creates a "SingleStore Workspace" shortcut on the desktop and in
    the Start menu, and opens the workspace. Run it again to update.

    One-line install (PowerShell or the Windows Run box):

        powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/jonasliesas/singlestore-mcp-server/master/install.ps1 | iex"

    With options:

        & ([scriptblock]::Create((irm https://raw.githubusercontent.com/jonasliesas/singlestore-mcp-server/master/install.ps1))) -Notebook -Database SASDP

    No administrator rights are needed: everything goes to your user profile.
    Connections (host, user, password or SSO) are set up in the workspace's
    Connections view; passwords go to Windows Credential Manager.

.PARAMETER InstallDir
    Where the program goes. Default: %USERPROFILE%\singlestore-workspace

.PARAMETER Database
    Database the shortcut opens in, e.g. SASDP.

.PARAMETER Notebook
    Also install the notebook's Python environment now (about 150 MB, a few
    minutes; otherwise the Notebook view offers it the first time).

.PARAMETER WithClaude
    Also register the SingleStore MCP server and skills with Claude Code, if it's installed.

.PARAMETER NoLaunch
    Don't open the workspace at the end.

.PARAMETER Source
    Install from a local copy of the project (a folder, e.g. on a share or USB stick)
    instead of downloading it from GitHub.

.PARAMETER ShortcutName
    Name of the shortcuts. Default: SingleStore Workspace

.PARAMETER Uninstall
    Remove the program and its shortcuts. Your connections, SQL files and
    notebooks are kept (in %USERPROFILE%\.singlestore-mcp and Documents).
#>
param(
    [string]$InstallDir = (Join-Path $HOME "singlestore-workspace"),
    [string]$Database = "",
    [switch]$Notebook,
    [switch]$WithClaude,
    [switch]$NoLaunch,
    [switch]$Uninstall,
    [string]$Source = "",
    [string]$ShortcutName = "SingleStore Workspace",
    [string]$Repo = "https://github.com/jonasliesas/singlestore-mcp-server",
    [string]$Branch = "master"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is much faster without the progress bar

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Info($text) { Write-Host "    $text" }
function Fail($text) { Write-Host ""; Write-Host "ERROR: $text" -ForegroundColor Red; throw $text }

function Stop-Workspace {
    # The workspace server runs from the program's .venv, whose pythonw.exe is a launcher that starts the
    # real Python as a child process: stop both, so the files can be replaced.
    $venv = Join-Path $InstallDir ".venv"
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $launchers = @($all | Where-Object { $_.ExecutablePath -and $_.CommandLine -and
        $_.ExecutablePath.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase) -and
        $_.CommandLine -like "*singlestore_mcp.workspace_app*" })
    if ($launchers.Count -eq 0) { return }
    $ids = @($launchers | ForEach-Object { $_.ProcessId })
    $children = @($all | Where-Object { $ids -contains $_.ParentProcessId })
    Info "Stopping the running workspace"
    foreach ($p in ($children + $launchers)) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 800
}

function Shortcut-Paths {
    @(
        (Join-Path ([Environment]::GetFolderPath("Desktop")) "$ShortcutName.lnk"),
        (Join-Path ([Environment]::GetFolderPath("Programs")) "$ShortcutName.lnk")
    )
}

# ---------------------------------------------------------------- uninstall
if ($Uninstall) {
    Step "Removing the SingleStore Workspace"
    Stop-Workspace
    foreach ($lnk in Shortcut-Paths) {
        if (Test-Path $lnk) { Remove-Item $lnk -Force; Info "Removed $lnk" }
    }
    if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force; Info "Removed $InstallDir" }
    Write-Host ""
    Write-Host "Done. Kept your connections and notebook environment in $(Join-Path $HOME '.singlestore-mcp')" -ForegroundColor Green
    Write-Host "(delete that folder too to remove everything; saved passwords are in Windows Credential Manager under 'singlestore-mcp')."
    return
}

Write-Host "SingleStore Workspace installer" -ForegroundColor Green
Info "Program folder: $InstallDir"

# ---------------------------------------------------------------- 1. uv (brings Python)
Step "Checking for uv (it installs Python 3.12 and the dependencies)"
$uvDirs = @((Join-Path $HOME ".local\bin"), (Join-Path $HOME ".cargo\bin"), (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links"))
foreach ($d in $uvDirs) { if ((Test-Path (Join-Path $d "uv.exe")) -and ($env:Path -notlike "*$d*")) { $env:Path = "$d;$env:Path" } }
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Info "Installing uv from astral.sh"
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Info "That didn't work ($($_.Exception.Message)); trying winget"
        winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements | Out-Null
    }
    foreach ($d in $uvDirs) { if ((Test-Path (Join-Path $d "uv.exe")) -and ($env:Path -notlike "*$d*")) { $env:Path = "$d;$env:Path" } }
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) { Fail "Couldn't install uv. Install it from https://docs.astral.sh/uv/ and run this again." }
}
$uv = $uv.Source
Info "uv: $uv"

# ---------------------------------------------------------------- 2. the program
Step "Getting the latest version"
Stop-Workspace
$git = Get-Command git -ErrorAction SilentlyContinue
if ($Source) {
    if (-not (Test-Path (Join-Path $Source "pyproject.toml"))) { Fail "$Source doesn't look like the project folder (no pyproject.toml)." }
    New-Item -ItemType Directory -Force $InstallDir | Out-Null
    & robocopy $Source $InstallDir /E /XD .venv .git __pycache__ node_modules /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "Copying $Source to $InstallDir failed (robocopy exit $LASTEXITCODE)." }
} elseif (Test-Path (Join-Path $InstallDir ".git")) {
    if (-not $git) { Fail "$InstallDir is a git checkout, but git isn't available." }
    & git -C $InstallDir pull --ff-only
    if ($LASTEXITCODE -ne 0) { Fail "git pull failed in $InstallDir (local changes?)." }
} elseif ($git -and -not (Test-Path $InstallDir)) {
    & git clone --depth 1 --branch $Branch "$Repo.git" $InstallDir
    if ($LASTEXITCODE -ne 0) { Fail "git clone of $Repo failed." }
} else {
    # No git: download the branch as a ZIP and copy it over the program folder (keeps .venv).
    $zip = Join-Path $env:TEMP "singlestore-workspace.zip"
    $unpack = Join-Path $env:TEMP "singlestore-workspace-src"
    Invoke-WebRequest "$Repo/archive/refs/heads/$Branch.zip" -OutFile $zip -UseBasicParsing
    if (Test-Path $unpack) { Remove-Item $unpack -Recurse -Force }
    Expand-Archive $zip -DestinationPath $unpack -Force
    $src = Get-ChildItem $unpack -Directory | Select-Object -First 1
    New-Item -ItemType Directory -Force $InstallDir | Out-Null
    & robocopy $src.FullName $InstallDir /E /XD .venv /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "Copying the program to $InstallDir failed (robocopy exit $LASTEXITCODE)." }
    Remove-Item $zip, $unpack -Recurse -Force -ErrorAction SilentlyContinue
}
Info "Program in $InstallDir"

# ---------------------------------------------------------------- 3. dependencies
Step "Installing Python 3.12 and the dependencies (first time: a minute or two)"
& $uv sync --directory $InstallDir --frozen --no-dev
if ($LASTEXITCODE -ne 0) {
    Stop-Workspace
    & $uv sync --directory $InstallDir --frozen --no-dev
    if ($LASTEXITCODE -ne 0) { Fail "Installing the dependencies failed. Close any SingleStore Workspace windows and run this again." }
}
$python = Join-Path $InstallDir ".venv\Scripts\python.exe"
$pythonw = Join-Path $InstallDir ".venv\Scripts\pythonw.exe"

# ---------------------------------------------------------------- 4. notebook environment (optional)
if ($Notebook) {
    Step "Installing the notebook's Python environment (about 150 MB)"
    & $python -m singlestore_mcp.notebook_kernel setup
    if ($LASTEXITCODE -ne 0) { Info "The notebook environment didn't install; the Notebook view offers it again later." }
}

# ---------------------------------------------------------------- 5. shortcuts
Step "Creating the shortcuts"
$shortcutArgs = @("$InstallDir\scripts\make_shortcut.py", "--name", $ShortcutName)
if ($Database) { $shortcutArgs += @("--database", $Database) }
& $python @shortcutArgs
if ($LASTEXITCODE -ne 0) { Fail "Creating the desktop shortcut failed." }
$desktopLnk, $startLnk = Shortcut-Paths
Copy-Item $desktopLnk $startLnk -Force
Info "Desktop and Start menu: $ShortcutName"

# ---------------------------------------------------------------- 6. Claude (optional)
if ($WithClaude) {
    Step "Registering with Claude Code"
    $claude = Get-Command claude -ErrorAction SilentlyContinue
    if ($claude) {
        & claude mcp remove singlestore -s user 2>$null | Out-Null
        & claude mcp add singlestore -s user -- $uv run --no-sync --directory $InstallDir singlestore-mcp-server
        Info "MCP server 'singlestore' added for your user; restart Claude to load it."
    } else {
        Info "The Claude Code command line (claude) isn't installed; skipping. See the README's Setup section."
    }
    $skills = Join-Path $HOME ".claude\skills"
    New-Item -ItemType Directory -Force $skills | Out-Null
    Get-ChildItem (Join-Path $InstallDir "claude-skills") -Directory | ForEach-Object {
        Copy-Item $_.FullName $skills -Recurse -Force
    }
    Info "Skills copied to $skills"
}

# ---------------------------------------------------------------- 7. open it
$hasConnections = (Test-Path (Join-Path $HOME ".singlestore-mcp\connections.json")) -or $env:SINGLESTORE_HOST -or $env:SINGLESTORE_URL
Write-Host ""
Write-Host "Done! Open '$ShortcutName' from the desktop or the Start menu." -ForegroundColor Green
if (-not $hasConnections) { Write-Host "First time: add your SingleStore connection in the Connections view (it opens now)." }
Write-Host "To update later, run the same install command again."
if (-not $NoLaunch) {
    $launch = @("-m", "singlestore_mcp.workspace_app")
    if ($Database) { $launch += @("--database", $Database) }
    if (-not $hasConnections) { $launch += @("--view", "connections") }
    Start-Process -FilePath $pythonw -ArgumentList $launch -WorkingDirectory $InstallDir
}
