#!/bin/sh
# Build the downloadable disk image: dist/Rozan-<version>-arm64.dmg with Rozan.app and an Applications link.
#   scripts/make_dmg.sh            (PA_SIGN_IDENTITY="" for ad-hoc signing, e.g. on CI)
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
"$ROOT/scripts/build_mac_app.sh" --release
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml")"
STAGE="$(mktemp -d)/Rozan"
mkdir -p "$STAGE" "$ROOT/dist"
cp -R "$ROOT/macos/build/Rozan.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
OUT="$ROOT/dist/Rozan-$VERSION-arm64.dmg"
rm -f "$OUT"
hdiutil create -volname "Rozan" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$OUT" >/dev/null
echo "built $OUT ($(du -h "$OUT" | cut -f1))"
