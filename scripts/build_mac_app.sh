#!/bin/sh
# Build Rozan.app (the bubble shell) into macos/build/. Usage: scripts/build_mac_app.sh [--install]
# --install copies it to ~/Applications.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/macos"
swift build -c release 2>&1 | tail -3
APP="$ROOT/macos/build/Rozan.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/PersonalAgent "$APP/Contents/MacOS/Rozan"
cp Resources/*.svg "$APP/Contents/Resources/"
# app icon for the Dock and Finder
ICONSET="$ROOT/macos/.build/AppIcon.iconset"
rm -rf "$ICONSET" && swift "$ROOT/scripts/make_icon.swift" iconset "$ICONSET"
swift "$ROOT/scripts/make_icon.swift" extension "$ROOT/extension/icons"
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
  <key>PAProjectRoot</key><string>$ROOT</string>
  <key>NSMicrophoneUsageDescription</key><string>Rozan listens only while you dictate a request (⌃⌥D). Audio stays on this Mac.</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>Your dictation is turned into text on this Mac, with Apple's on-device recognition only.</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
# Sign with a stable local identity when there is one, so macOS keeps granted permissions across rebuilds.
IDENTITY="${PA_SIGN_IDENTITY:-$(security find-identity -v -p codesigning | sed -n 's/.*"\(.*\)"/\1/p' | head -1)}"
if [ -n "$IDENTITY" ]; then
  codesign --force --sign "$IDENTITY" "$APP" >/dev/null 2>&1 && echo "signed with: $IDENTITY"
else
  codesign --force --sign - "$APP" >/dev/null 2>&1 || true
fi
echo "built $APP"
if [ "$1" = "--install" ]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/Rozan.app"
  cp -R "$APP" "$HOME/Applications/"
  echo "installed $HOME/Applications/Rozan.app"
fi
