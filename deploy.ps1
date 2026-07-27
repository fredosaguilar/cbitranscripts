# Manual deploy, for when the GitHub Action is not being used.
# Usage:  powershell -ExecutionPolicy Bypass -File deploy.ps1
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$target = "C:\cbi-deploy"
Write-Host "Fetching the current master branch..."
Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $target | Out-Null
Invoke-WebRequest -UseBasicParsing `
    -Uri "https://github.com/fredosaguilar/cbitranscripts/archive/refs/heads/master.zip" `
    -OutFile "$target\src.zip"
Expand-Archive -Path "$target\src.zip" -DestinationPath $target -Force
Copy-Item -Path "$target\cbitranscripts-master\*" -Destination $target -Recurse -Force
Remove-Item -Recurse -Force "$target\cbitranscripts-master", "$target\src.zip"
Set-Location $target

$head = (Invoke-WebRequest -UseBasicParsing `
    -Uri "https://api.github.com/repos/fredosaguilar/cbitranscripts/commits/master").Content |
    ConvertFrom-Json
Write-Host "Deploying: $($head.commit.message.Split("`n")[0])" -ForegroundColor Cyan

railway up
