# chaosRouter Windows build — produces dist/chaosRouter/chaosRouter.exe
# Requires: pip install pyinstaller pyside6
# Optional installer: compile build/chaosrouter.iss with Inno Setup 6 afterwards.

$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\.."

pyinstaller --noconfirm --clean --windowed --name chaosRouter `
    --collect-submodules chaosrouter `
    --collect-all numba --collect-all llvmlite `
    --copy-metadata numba --copy-metadata llvmlite `
    --hidden-import matplotlib.backends.backend_agg `
    chaosRouter.py

Write-Host ""
Write-Host "Build done: dist\chaosRouter\chaosRouter.exe"
Write-Host "Installer:  open build\chaosrouter.iss in Inno Setup and compile."
