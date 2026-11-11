param(
    [int]$ParentId,
    [string]$AppExe,
    [string]$Installer,
    [string]$ExpectedHash,
    [string]$Version,
    [switch]$Tray
)
$ErrorActionPreference = "Stop"
$status = "installed"
$detail = ""
try {
    $parent = Get-Process -Id $ParentId -ErrorAction SilentlyContinue
    if ($parent -and -not $parent.WaitForExit(30000)) { throw "Timeout waiting for application." }
    if ((Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash -ne $ExpectedHash) {
        throw "SHA-256 verification failed."
    }
    $target = Split-Path -Parent $AppExe
    $log = Join-Path $PSScriptRoot "install.log"
    $arguments = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOCLOSEAPPLICATIONS",
                   ('/DIR="' + $target + '"'), ('/LOG="' + $log + '"'))
    $setup = Start-Process -FilePath $Installer -ArgumentList $arguments -PassThru -Wait -WindowStyle Hidden
    if ($setup.ExitCode -ne 0) { throw "Installer returned $($setup.ExitCode)." }
    Remove-Item -LiteralPath $Installer -ErrorAction SilentlyContinue
}
catch {
    $status = "failed"
    $detail = $_.Exception.Message
}
finally {
    $report = Join-Path $PSScriptRoot "result.json"
    @{ version = $Version; status = $status; detail = $detail } | ConvertTo-Json -Compress |
        Set-Content -Encoding UTF8 -LiteralPath "$report.tmp"
    Move-Item -Force -LiteralPath "$report.tmp" -Destination $report
    if (Test-Path -LiteralPath $AppExe) {
        if ($Tray) {
            Start-Process -FilePath $AppExe -ArgumentList "--tray" -WindowStyle Hidden
        } else {
            # The application itself decides whether its Qt window should be visible.
            Start-Process -FilePath $AppExe -WindowStyle Normal
        }
    }
}
