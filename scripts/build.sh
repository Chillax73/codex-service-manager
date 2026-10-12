#!/bin/sh
set -eu
CSM_BUILD_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CSM_BUNDLE="$CSM_BUILD_ROOT/build/Codex Service Manager.app"
mkdir -p "$CSM_BUNDLE/Contents/MacOS" "$CSM_BUNDLE/Contents/Resources/backend" "$CSM_BUNDLE/Contents/Resources/scripts" "$CSM_BUILD_ROOT/build/module-cache"
swiftc -swift-version 5 -target "$(uname -m)-apple-macos13.0" -O -module-cache-path "$CSM_BUILD_ROOT/build/module-cache" -framework SwiftUI -framework AppKit "$CSM_BUILD_ROOT/app/Sources/ServiceManager.swift" -o "$CSM_BUNDLE/Contents/MacOS/ServiceManager"
cp "$CSM_BUILD_ROOT/backend/engine.py" "$CSM_BUILD_ROOT/backend/cli.py" "$CSM_BUILD_ROOT/backend/capture.py" "$CSM_BUNDLE/Contents/Resources/backend/"
cp "$CSM_BUILD_ROOT/scripts/csm" "$CSM_BUNDLE/Contents/Resources/scripts/"
cat > "$CSM_BUNDLE/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>ServiceManager</string>
<key>CFBundleIdentifier</key><string>local.codex-service-manager</string>
<key>CFBundleName</key><string>Codex Service Manager</string>
<key>CFBundleDisplayName</key><string>Codex Service Manager</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundleShortVersionString</key><string>0.1.0</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
/usr/bin/codesign --force --sign - "$CSM_BUNDLE"
printf '%s\n' "$CSM_BUNDLE"
