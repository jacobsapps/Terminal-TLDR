#!/bin/bash
set -euo pipefail
case "${1:-}" in ""|--install) ;; *) echo 'Usage: scripts/build-app.sh [--install]' >&2; exit 2;; esac
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"
ARCH="$(uname -m)"
BUILD="$ROOT/.build"
APP="$ROOT/dist/Terminal TLDR.app"
mkdir -p "$BUILD" "$ROOT/dist"
if [ ! -x "$BUILD/packaging/bin/python" ]; then
  "$PYTHON" -m venv "$BUILD/packaging"
fi
"$BUILD/packaging/bin/python" -m pip install -r macos/requirements-build.txt
"$BUILD/packaging/bin/pyinstaller" --noconfirm --onedir --name terminal-tldr \
  --distpath "$BUILD/helper-dist" --workpath "$BUILD/helper-work" --specpath "$BUILD" \
  --paths "$ROOT/lib" --hidden-import iterm2 --collect-all iterm2 --collect-all websockets \
  --add-data "$ROOT/iterm2/terminal_tldr_iterm.py:iterm2" \
  --add-data "$ROOT/lib/titles.py:lib" --add-data "$ROOT/lib/agent_titles.py:lib" --add-data "$ROOT/lib/setup.py:lib" \
  --add-data "$ROOT/lib/conversations.py:lib" \
  bin/terminal-tldr
# Replace only this build's output bundle, never an installed application.
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp macos/Info.plist "$APP/Contents/Info.plist"
cp -R "$BUILD/helper-dist/terminal-tldr" "$APP/Contents/Resources/runtime"
swift build -c release
BIN="$(swift build -c release --show-bin-path)/TerminalTLDR"
cp "$BIN" "$APP/Contents/MacOS/TerminalTLDR"
swift -module-cache-path "$BUILD/module-cache" macos/Icon.swift "$BUILD/AppIcon.iconset"
iconutil -c icns "$BUILD/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
"$BUILD/packaging/bin/python" scripts/bundle-licenses.py "$APP/Contents/Resources/Licenses"
cp macos/THIRD-PARTY-NOTICES.md "$APP/Contents/Resources/THIRD-PARTY-NOTICES.md"
# Ad-hoc for local builds. Release signing and notarization are separate steps.
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"
"$APP/Contents/Resources/runtime/terminal-tldr" doctor
(cd dist && ditto -c -k --keepParent 'Terminal TLDR.app' "Terminal-TLDR-$ARCH.zip")
printf '\nBuilt: %s\nArchive: %s/dist/Terminal-TLDR-%s.zip\n' "$APP" "$ROOT" "$ARCH"
if [[ "${1:-}" == --install ]]; then
  bash scripts/install-app.sh
fi
