param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [string]$ISCC = "",
    [switch]$OnedirOnly
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Confirm-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed ($LASTEXITCODE)." }
}

$version = & $Python -c "from yt_downloader import APP_VERSION; print(APP_VERSION)"
Confirm-Exit "Reading app version"
& $Python -m yt_downloader --prepare-seed vendor\seed
Confirm-Exit "Preparing verified dependencies"
& $Python packaging\assets.py
Confirm-Exit "Preparing assets"
& $Python -m PyInstaller --noconfirm --clean packaging\YTDownloader.spec
Confirm-Exit "Building onedir"

if (-not $OnedirOnly) {
    if (-not $ISCC) {
        $compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($compiler) { $ISCC = $compiler.Source }
    }
    if (-not $ISCC) {
        $candidates = foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
            foreach ($directory in @("Inno Setup 7", "Inno Setup 6", "Programs\Inno Setup 7", "Programs\Inno Setup 6")) {
                Join-Path (Join-Path $root $directory) "ISCC.exe"
            }
        }
        $ISCC = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
    if (-not $ISCC) { throw "Install Inno Setup 6.3+ or pass -ISCC with the path to ISCC.exe." }
    & $ISCC /Qp "/DAppVersion=$version" packaging\setup.iss
    Confirm-Exit "Building installer"
    $installer = "dist\YTDownloader-Setup.exe"
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
    Set-Content -Encoding ascii -LiteralPath "$installer.sha256" -Value "$hash  YTDownloader-Setup.exe"
}
