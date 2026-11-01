$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
Write-Host 'Setup complete. Set OPENAI_API_KEY and VIDEO_MANUAL_FFMPEG_DIR for generation. Double-click 起動.cmd.'
