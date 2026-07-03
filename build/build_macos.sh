#!/usr/bin/env bash
# chaosRouter macOS build — produces dist/chaosRouter.app and a zip.
# Requires: pip install pyinstaller pyside6
set -euo pipefail
cd "$(dirname "$0")/.."

pyinstaller --noconfirm --clean --windowed --name chaosRouter \
    --icon chaosrouter/assets/icon.icns \
    --add-data "chaosrouter/assets:chaosrouter/assets" \
    --collect-submodules chaosrouter \
    --collect-all numba --collect-all llvmlite \
    --copy-metadata numba --copy-metadata llvmlite \
    --hidden-import matplotlib.backends.backend_agg \
    --osx-bundle-identifier org.chaosrouter.app \
    chaosRouter.py

cd dist
zip -r chaosRouter-macos.zip chaosRouter.app
echo ""
echo "Build done: dist/chaosRouter.app  (zip: dist/chaosRouter-macos.zip)"
echo "For distribution outside the App Store, sign and notarize:"
echo "  codesign --deep --force --sign 'Developer ID Application: ...' chaosRouter.app"
echo "  xcrun notarytool submit chaosRouter-macos.zip --keychain-profile ..."
