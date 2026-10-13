# GLASS engine setup — creates the local Python environment and downloads the free local models.
# Safe to re-run. Nothing here requires an account, API key or payment.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $env:LOCALAPPDATA 'GLASS\venv'
# a venv next to the source checkout (developer setup) is used as-is
if (Test-Path (Join-Path $here '.venv\Scripts\python.exe')) { $venv = Join-Path $here '.venv' }

function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }

Step 'Python 3.11'
$py = $null
try { $py = (& py -3.11 -c "import sys; print(sys.executable)") 2>$null } catch {}
if (-not $py) {
  Write-Host 'Python 3.11 not found. Installing with winget (user scope)...'
  winget install -e --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements
  $py = (& py -3.11 -c "import sys; print(sys.executable)")
}
Write-Host "Using $py"

Step "Virtual environment: $venv"
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) { & $py -m venv $venv }
$vpy = Join-Path $venv 'Scripts\python.exe'
& $vpy -m pip install --upgrade pip --quiet
& $vpy -m pip install -r (Join-Path $here 'requirements.txt')

Step 'Tesseract OCR (used to double-check maths lines; optional)'
if (-not (Test-Path 'C:\Program Files\Tesseract-OCR\tesseract.exe')) {
  try { winget install -e --id UB-Mannheim.TesseractOCR --accept-package-agreements --accept-source-agreements } catch { Write-Host 'Skipped (Windows OCR alone still works).' }
}

Step 'Ollama (local language models)'
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
  winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
  $env:Path += ";$env:LOCALAPPDATA\Programs\Ollama"
}
Write-Host 'Downloading the answer model (~2.5 GB) and the vision model (~3.3 GB)...'
ollama pull qwen3:4b-instruct
ollama pull qwen3-vl:4b-instruct

Step 'Speech model (Whisper large-v3-turbo, ~1.6 GB) and GPU check'
& $vpy -c "import sys; sys.path.insert(0, r'$here'); from glass.audio.asr import ASRWorker; a = ASRWorker(lambda s: None, lambda s, i: print(s, i)); a.load('large-v3-turbo')"

Write-Host "`nGLASS engine is ready. Start GLASS from the Start menu." -ForegroundColor Green
