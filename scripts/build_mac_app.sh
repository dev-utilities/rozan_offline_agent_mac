#!/bin/sh
# Build Rozan.app into macos/build/.
#   scripts/build_mac_app.sh             development build: runs the code in this folder (and its .venv)
#   scripts/build_mac_app.sh --release   self-contained build for the .dmg: the code is copied into the app,
#                                        and the Setup window creates its Python environment on first launch
#   --install                            also copies the app to ~/Applications
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RELEASE=0; INSTALL=0
for a in "$@"; do case "$a" in --release) RELEASE=1;; --install) INSTALL=1;; esac; done
cd "$ROOT/macos"
LOG="$(mktemp)"
swift build -c release > "$LOG" 2>&1 || { grep -E "error:" "$LOG" | grep -v SwiftDriver | head -20; echo "Swift build failed"; exit 1; }
tail -1 "$LOG"
APP="$ROOT/macos/build/Rozan.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/PersonalAgent "$APP/Contents/MacOS/Rozan"
cp Resources/*.svg "$APP/Contents/Resources/"
# app icon for the Dock and Finder
ICONSET="$ROOT/macos/.build/AppIcon.iconset"
rm -rf "$ICONSET" && swift "$ROOT/scripts/make_icon.swift" iconset "$ICONSET"
swift "$ROOT/scripts/make_icon.swift" extension "$ROOT/extension/icons"
swift "$ROOT/scripts/make_icon.swift" bubble "$APP/Contents/Resources"
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
# small native helpers the Python core calls (run as children of the app, so they share its permissions)
mkdir -p "$ROOT/bin"
for h in "$ROOT"/helpers/*.swift; do
  swiftc -O -o "$ROOT/bin/$(basename "$h" .swift)" "$h"
done
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.personal-agent.shell</string><!-- kept: macOS permissions are tied to it -->
  <key>CFBundleName</key><string>Rozan</string>
  <key>CFBundleDisplayName</key><string>Rozan</string>
  <key>CFBundleExecutable</key><string>Rozan</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><false/>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>NSHighResolutionCapable</key><true/>
$( [ "$RELEASE" = 1 ] || echo "  <key>PAProjectRoot</key><string>$ROOT</string>" )
  <key>NSMicrophoneUsageDescription</key><string>Rozan listens only while you dictate a request (⌃⌥D). Audio stays on this Mac.</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>Your dictation is turned into text on this Mac, with Apple's on-device recognition only.</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
if [ "$RELEASE" = 1 ]; then
  # the core, read-only inside the app (Setup puts the Python environment in Application Support)
  CORE="$APP/Contents/Resources/core"
  mkdir -p "$CORE/scripts" "$CORE/bridge"
  rsync -a --exclude __pycache__ "$ROOT/agent" "$ROOT/ui" "$ROOT/extension" "$ROOT/examples" "$ROOT/bin" "$CORE/"
  cp "$ROOT/policy.yaml" "$ROOT/requirements.txt" "$CORE/"
  cp "$ROOT/bridge/__init__.py" "$ROOT/bridge/native_host.py" "$CORE/bridge/"
  cp "$ROOT/scripts/install_chrome_bridge.py" "$CORE/scripts/"
fi
# Sign with a stable local identity when there is one, so macOS keeps granted permissions across rebuilds.
IDENTITY="${PA_SIGN_IDENTITY-$(security find-identity -v -p codesigning | sed -n 's/.*"\(.*\)"/\1/p' | head -1)}"
if [ -n "$IDENTITY" ]; then
  codesign --force --sign "$IDENTITY" "$APP" >/dev/null 2>&1 && echo "signed with: $IDENTITY"
else
  codesign --force --sign - "$APP" >/dev/null 2>&1 || true
fi
echo "built $APP"
if [ "$INSTALL" = 1 ]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/Rozan.app"
  cp -R "$APP" "$HOME/Applications/"
  echo "installed $HOME/Applications/Rozan.app"
fi
