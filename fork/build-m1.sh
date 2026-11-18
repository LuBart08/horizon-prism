#!/bin/sh
# Configure and build the Horizon Prism Launcher fork (milestone M1).
#
# Requirements (user-space install used on this machine):
#   - Qt 6.8+ via aqtinstall:   aqt install-qt mac desktop 6.8.3 clang_64 \
#                                   --modules qtnetworkauth qtimageformats qtshadertools -O ~/Qt
#   - CMake + Ninja via pip:    python3 -m pip install --user cmake ninja
#   - pkg-config:               built from pkgconf sources into ~/.local
#   - vcpkg dependencies:       cmake/vcpkg/vcpkg install (manifest mode)
#
# Override QT_DIR, BUILD_DIR, HORIZON_FORK (ON for M6 branding), BUILD_TESTING
# or TARGET to change the defaults.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PRISM="$ROOT/PrismLauncher"
QT_DIR=${QT_DIR:-"$HOME/Qt/6.8.3/macos"}
BUILD_DIR=${BUILD_DIR:-"$ROOT/build-horizon"}
HORIZON_FORK=${HORIZON_FORK:-OFF}
BUILD_TESTING=${BUILD_TESTING:-OFF}
if [ "$HORIZON_FORK" = "ON" ]; then
    APP_NAME=HorizonPrismLauncher
    BINARY_NAME=horizon-prism
else
    APP_NAME=PrismLauncher
    BINARY_NAME=prismlauncher
fi
TARGET=${TARGET:-"$APP_NAME"}

if [ ! -d "$PRISM" ]; then
    echo "error: $PRISM not found; clone the fork there first" >&2
    exit 1
fi
if [ ! -d "$QT_DIR" ]; then
    echo "error: Qt not found at $QT_DIR (set QT_DIR)" >&2
    exit 1
fi
command -v cmake >/dev/null || { echo "error: cmake not found" >&2; exit 1; }
command -v ninja >/dev/null || { echo "error: ninja not found" >&2; exit 1; }
command -v pkg-config >/dev/null || { echo "error: pkg-config not found" >&2; exit 1; }

# macOS 26 SDK removed AGL.framework, but Qt 6.8 still links `-framework AGL`.
# Prism uses no AGL symbol, so an empty static framework is enough to link.
STUBS="$ROOT/stubs"
AGL="$STUBS/AGL.framework"
if [ ! -e "$AGL/Versions/A/AGL" ]; then
    mkdir -p "$AGL/Versions/A" "$AGL/Resources"
    printf 'static const int horizon_agl_stub = 0;\n' > "$STUBS/agl_stub.c"
    clang -c -o "$STUBS/agl_stub.o" "$STUBS/agl_stub.c"
    ar rcs "$AGL/Versions/A/AGL" "$STUBS/agl_stub.o"
    ln -sfn A "$AGL/Versions/Current"
    ln -sfn Versions/Current/AGL "$AGL/AGL"
    cat > "$AGL/Resources/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>org.horizon-prism.agl-stub</string>
  <key>CFBundleName</key><string>AGL</string>
  <key>CFBundlePackageType</key><string>FMWK</string>
</dict></plist>
PLIST
fi

cmake -S "$PRISM" -B "$BUILD_DIR" -G Ninja \
    -DCMAKE_BUILD_TYPE=Debug \
    -DCMAKE_PREFIX_PATH="$QT_DIR" \
    -DCMAKE_LIBRARY_PATH="$ROOT/stubs" \
    -U WrapOpenGL_AGL \
    -DCMAKE_TOOLCHAIN_FILE="$PRISM/cmake/vcpkg/scripts/buildsystems/vcpkg.cmake" \
    -DVCPKG_TARGET_TRIPLET=x64-osx \
    -DENABLE_LTO=OFF \
    -DHORIZON_FORK="$HORIZON_FORK" \
    -DBUILD_TESTING="$BUILD_TESTING"

cmake --build "$BUILD_DIR" --target "$TARGET" -j "$(sysctl -n hw.ncpu)"

echo
echo "Built: $BUILD_DIR/$APP_NAME.app (binary: $BINARY_NAME)"
