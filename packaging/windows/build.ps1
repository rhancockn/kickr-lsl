# Build the Wahoo LSL installer: clean venv -> pip install .[windows] -> PyInstaller -> Inno Setup.
# Requires Windows, Python 3.12 (py launcher) and Inno Setup 6.3+ or 7. Output: build\installer\WahooLSL-Setup-<version>.exe
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path "$PSScriptRoot\..\..")

function Run($exe) {
    # Windows PowerShell 5.1 turns any stderr output into a terminating error under "Stop";
    # judge success by the exit code only.
    $ErrorActionPreference = "Continue"
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
$m = Select-String -Path pyproject.toml -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $m) { throw "Cannot read the version from pyproject.toml" }
$version = $m.Matches[0].Groups[1].Value

$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    $candidates = foreach ($root in $env:ProgramFiles, ${env:ProgramFiles(x86)}) {
        foreach ($v in 7, 6) { if ($root) { Join-Path $root "Inno Setup $v\ISCC.exe" } }
    }
    $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { throw "Inno Setup not found: put ISCC.exe on PATH or install Inno Setup 7 or 6" }
}
Write-Host "Using $iscc, version $version"
Run $iscc "/DAppVersion=$version" packaging\windows\wahoo.iss
Write-Host "Installer: build\installer\WahooLSL-Setup-$version.exe"
