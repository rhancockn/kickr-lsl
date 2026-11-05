# Build the Wahoo LSL installer: clean venv -> pip install .[windows] -> PyInstaller -> Inno Setup.
# Requires Windows, Python 3.12 (py launcher) and Inno Setup 6. Output: build\installer\WahooLSL-Setup-<version>.exe
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path "$PSScriptRoot\..\..")

function Run($exe) {
    & $exe @args
    if ($LASTEXITCODE -ne 0) { throw "$exe $args failed with exit code $LASTEXITCODE" }
}

$venv = "build\venv"
if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
Run py -3.12 -m venv $venv
$python = "$venv\Scripts\python.exe"
Run $python -m pip install --upgrade pip
Run $python -m pip install ".[windows]"
Run $python -m PyInstaller --noconfirm --distpath build\pyinstaller\dist --workpath build\pyinstaller\work packaging\windows\wahoo.spec
$version = & $python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"

$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
Run $iscc "/DAppVersion=$version" packaging\windows\wahoo.iss
Write-Host "Installer: build\installer\WahooLSL-Setup-$version.exe"
