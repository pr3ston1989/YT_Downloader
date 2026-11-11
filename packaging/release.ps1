param([string]$Python = ".\.venv\Scripts\python.exe")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$version = & $Python -c "from yt_downloader import APP_VERSION; print(APP_VERSION)"
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw "Invalid app version." }
if (git status --porcelain) { throw "Commit pending changes before releasing." }
$tag = "v$version"
git tag $tag
if ($LASTEXITCODE -ne 0) { throw "Could not create tag $tag." }
git push origin "refs/tags/$tag"
if ($LASTEXITCODE -ne 0) { throw "Tag created locally. Retry with git push origin refs/tags/$tag." }
