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
# Override QT_DIR or BUILD_DIR to change the defaults.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PRISM="$ROOT/PrismLauncher"
QT_DIR=${QT_DIR:-"$HOME/Qt/6.8.3/macos"}
BUILD_DIR=${BUILD_DIR:-"$ROOT/build-horizon"}

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

cmake -S "$PRISM" -B "$BUILD_DIR" -G Ninja \
    -DCMAKE_BUILD_TYPE=Debug \
    -DCMAKE_PREFIX_PATH="$QT_DIR" \
    -DCMAKE_TOOLCHAIN_FILE="$PRISM/cmake/vcpkg/scripts/buildsystems/vcpkg.cmake" \
    -DVCPKG_TARGET_TRIPLET=x64-osx \
    -DENABLE_LTO=OFF \
    -DBUILD_TESTING=OFF

cmake --build "$BUILD_DIR" --target PrismLauncher -j "$(sysctl -n hw.ncpu)"

echo
echo "Built: $BUILD_DIR/PrismLauncher.app"
