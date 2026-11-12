param([string]$Version = '1.1.0')

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    python -m PyInstaller --noconfirm MCTransmuteBot.spec
    if ($LASTEXITCODE -ne 0) {
        throw 'PyInstaller build failed.'
    }
    $package = Join-Path $PSScriptRoot "dist\mc-transmute-bot-$Version-windows-x64.zip"
    $application = Join-Path $PSScriptRoot 'dist\MCTransmuteBot'
    $staging = Join-Path $PSScriptRoot "build\portable-$Version-$([guid]::NewGuid().ToString('N'))"
    $images = Join-Path $staging 'docs'
    New-Item -ItemType Directory -Path $images -Force | Out-Null
    Copy-Item -Path (Join-Path $application '*') -Destination $staging -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination $staging
    foreach ($name in 'ui_preview.png', 'settings_preview.png', 'window_selection.png') {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot "docs\$name") -Destination $images
    }
    Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $package -Force
    $hash = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $([IO.Path]::GetFileName($package))" | Set-Content -LiteralPath "$package.sha256" -Encoding ascii
    Get-Item -LiteralPath $package, "$package.sha256" | Select-Object Name, Length
}
finally {
    Pop-Location
}
