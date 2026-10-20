#!/bin/bash
# Install this app only. Never restart iTerm or a coding agent.
set -euo pipefail
cd "$(dirname "$0")/.."
SOURCE="$PWD/dist/Terminal TLDR.app"
DEST="/Applications/Terminal TLDR.app"
IDENTIFIER="com.jacobsapps.TerminalTLDR"
check_target() {
  if pgrep -x TerminalTLDR >/dev/null; then
    echo 'Quit Terminal TLDR before updating the app. Leave iTerm open.' >&2; exit 1
  else
    [[ $? == 1 ]] || { echo 'Cannot check whether the app is running.' >&2; exit 1; }
  fi
  if [[ -L "$DEST" || ( -e "$DEST" && ! -d "$DEST" ) ]]; then
    echo 'Install target is not a regular app bundle.' >&2; exit 1
  fi
  if [[ -d "$DEST" && "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$DEST/Contents/Info.plist")" != "$IDENTIFIER" ]]; then
    echo 'Install target belongs to another app.' >&2; exit 1
  fi
}
check_target
codesign --verify --deep --strict "$SOURCE"
if [[ -d "$DEST" ]]; then
  BACKUP="$HOME/Library/Application Support/Terminal TLDR/Application Backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"
  mkdir -p "$BACKUP"; chmod 700 "$BACKUP"
  ditto "$DEST" "$BACKUP/Terminal TLDR.app"
fi
STAGING="$(mktemp -d '/Applications/.terminal-tldr-install.XXXXXX')"
cleanup() {
  if [[ -d "$STAGING/previous.app" && ! -e "$DEST" ]]; then mv "$STAGING/previous.app" "$DEST"; fi
  rm -rf -- "$STAGING"
}
trap cleanup EXIT
ditto "$SOURCE" "$STAGING/new.app"
codesign --verify --deep --strict "$STAGING/new.app"
check_target
if [[ -d "$DEST" ]]; then mv "$DEST" "$STAGING/previous.app"; fi
mv "$STAGING/new.app" "$DEST"
codesign --verify --deep --strict "$DEST"
echo "Installed, not launched: $DEST"
