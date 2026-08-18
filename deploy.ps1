# Deploy the current master branch to Railway from this machine.
# Usage:  powershell -ExecutionPolicy Bypass -File deploy.ps1
# Requires the Railway CLI, signed in once with:  railway login
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repo = "fredosaguilar/cbitranscripts"
$target = "C:\cbi-deploy"

# Never delete the directory this shell is standing in. A previous run ends with
# Set-Location $target, so re-running from the same window left Remove-Item
# failing on a locked directory -- silently, because it was told to -- and the
# stale files underneath were then uploaded as though they were fresh.
Set-Location $env:USERPROFILE
if (Test-Path $target) { Remove-Item -Recurse -Force $target }
New-Item -ItemType Directory -Path $target | Out-Null

Write-Host "Fetching the current master branch..."

$commit = $null
$haveGit = [bool](Get-Command git -ErrorAction SilentlyContinue)
if ($haveGit) {
    git clone --quiet --depth 1 --branch master "https://github.com/$repo.git" $target
    if ($LASTEXITCODE -eq 0) {
        $commit = (git -C $target log -1 --pretty="%h %s")
        # Not worth uploading, and railway up would send the whole thing
        Remove-Item -Recurse -Force (Join-Path $target ".git")
    } else {
        Write-Host "git clone failed; falling back to a zip download." -ForegroundColor Yellow
    }
}

if (-not $commit) {
    # github.com/<repo>/archive is served through a CDN that has handed back a
    # stale master for minutes at a time -- long enough to deploy yesterday's
    # code twice and conclude the bug was somewhere else. codeload with a unique
    # query string is not cached the same way.
    $zip = Join-Path $target "src.zip"
    Invoke-WebRequest -UseBasicParsing -Headers @{ "Cache-Control" = "no-cache" } `
        -Uri "https://codeload.github.com/$repo/zip/refs/heads/master?nocache=$([guid]::NewGuid())" `
        -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $target -Force
    Copy-Item -Path (Join-Path $target "cbitranscripts-master\*") -Destination $target -Recurse -Force
    Remove-Item -Recurse -Force (Join-Path $target "cbitranscripts-master"), $zip
}

Set-Location $target

# Report what is ON DISK. The old script asked the GitHub API for the newest
# commit and printed that regardless of what it had actually downloaded, so a
# stale download still announced the right commit message -- which is precisely
# how a failed deploy passed for a good one.
if ($commit) {
    Write-Host "Deploying: $commit" -ForegroundColor Cyan
}
$stamp = (Get-FileHash (Join-Path $target "main.py") -Algorithm SHA256).Hash.Substring(0, 12)
Write-Host "main.py on disk: $stamp" -ForegroundColor Cyan

railway up

Write-Host ""
Write-Host "Confirm what is actually running:" -ForegroundColor Green
Write-Host "  https://cbitranscripts.up.railway.app/healthz" -ForegroundColor Green
Write-Host "Its build value changes whenever the source does. If it did not change," -ForegroundColor Green
Write-Host "the upload did not reach the service that serves the site." -ForegroundColor Green
