param(
    [string]$Python = "python"
)

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $root
try {
    & $Python -m pip install -e ".[audio,asr,wake,llm,tts,ui,agent,build]"
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    & $Python -m PyInstaller --noconfirm VoicePet.spec
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    $release = Join-Path $root "dist\VoicePet"
    foreach ($name in "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md") {
        Copy-Item -LiteralPath (Join-Path $root $name) -Destination $release -Force
    }
    Copy-Item -LiteralPath (Join-Path $root "packaging\licenses\LGPL-3.0.txt") -Destination $release -Force
}
finally {
    Pop-Location
}
