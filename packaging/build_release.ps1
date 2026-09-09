param(
    [string]$Python = "python"
)

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $root
try {
    & $Python -m pip install -e ".[audio,asr,wake,llm,tts,tools,ui,agent,build]"
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    & $Python -m PyInstaller --noconfirm VoicePet.spec
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}
