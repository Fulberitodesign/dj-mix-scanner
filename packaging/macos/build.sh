#!/bin/bash
# Construit « Mix Scanner.app » puis le DMG d'installation (à lancer sur un Mac).
# Le résultat est dans dist/ : MixScanner-<version>-<AppleSilicon|Intel>.dmg
set -euo pipefail
cd "$(dirname "$0")/../.."

PY=${PYTHON:-python3}
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r requirements.txt "yt-dlp[default]" pyinstaller deno pywebview

VERSION=$("$PY" -c "import mixscanner; print(mixscanner.__version__)")
case "$(uname -m)" in arm64) ARCH=AppleSilicon ;; *) ARCH=Intel ;; esac
DENO=$("$PY" -c "import shutil, sysconfig, os; print(shutil.which('deno') or os.path.join(sysconfig.get_path('scripts'), 'deno'))")
[ -x "$DENO" ] || { echo "deno introuvable"; exit 1; }

rm -rf build dist
# Identité de cette construction : sert à l'app pour savoir si une mise à jour existe.
SHA=${GITHUB_SHA:-$(git rev-parse HEAD 2>/dev/null || echo "")}
cat > mixscanner/_build.py <<PYEOF
BUILD_SHA = "$SHA"
BUILD_TIME = "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ARCH = "$ARCH"
PYEOF
"$PY" -m PyInstaller --noconfirm --clean --windowed --name "Mix Scanner" \
  --osx-bundle-identifier com.fulberitodesign.mixscanner \
  --icon packaging/macos/icon.icns \
  --add-data "mixscanner/static:mixscanner/static" \
  --add-binary "$DENO:bin" \
  --collect-all yt_dlp --collect-all yt_dlp_ejs --collect-all imageio_ffmpeg \
  --collect-all shazamio --collect-all shazamio_core --collect-data certifi \
  --collect-submodules uvicorn --collect-submodules mixscanner \
  --collect-data webview --hidden-import webview.platforms.cocoa \
  --hidden-import AppKit --hidden-import Foundation --hidden-import WebKit \
  packaging/mix_scanner_app.py

APP="dist/Mix Scanner.app"
if [ "$(uname)" = "Darwin" ]; then
  PLIST="$APP/Contents/Info.plist"
  plutil -replace CFBundleName -string "Mix Scanner" "$PLIST"
  plutil -replace CFBundleDisplayName -string "Mix Scanner" "$PLIST"
  plutil -replace NSHighResolutionCapable -bool true "$PLIST"
  plutil -replace CFBundleShortVersionString -string "$VERSION" "$PLIST"
  plutil -replace CFBundleVersion -string "$VERSION" "$PLIST"
  plutil -replace LSMinimumSystemVersion -string "11.0" "$PLIST"
  codesign --force --deep --sign - "$APP"                    # signature locale (obligatoire sur Apple Silicon)

  "$APP/Contents/MacOS/Mix Scanner" --selftest
  "$APP/Contents/MacOS/Mix Scanner" --selftest-window       # ouvre la vraie fenêtre, la vérifie, la ferme

  STAGE=dist/dmg
  mkdir -p "$STAGE"
  cp -R "$APP" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  cp packaging/macos/LISEZ-MOI.txt "$STAGE/"
  hdiutil create -volname "Mix Scanner" -srcfolder "$STAGE" -ov -format UDZO "dist/MixScanner-$VERSION-$ARCH.dmg"
  # Vérifie la mise à jour automatique sur ce Mac : installe ce DMG par-dessus une copie de l'app.
  "$APP/Contents/MacOS/Mix Scanner" --selftest-update "dist/MixScanner-$VERSION-$ARCH.dmg"
  echo "✅ dist/MixScanner-$VERSION-$ARCH.dmg"
else
  "dist/Mix Scanner/Mix Scanner" --selftest                  # essai sous Linux (pas de .app ni de DMG)
fi
