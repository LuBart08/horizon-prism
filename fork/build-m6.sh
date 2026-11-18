#!/bin/sh
# Build the fork with its own branding (milestone M6): horizon-prism.app,
# the `horizon-prism` binary, the dev.playhorizon.HorizonPrismLauncher bundle id
# and a separate data folder, so it can live beside Prism.
#
# Same requirements as build-m1.sh. Override BUILD_DIR or QT_DIR to change the
# defaults. The result is unsigned: signing and notarization are described in
# fork/M6.md and need secrets this script cannot create.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BUILD_DIR=${BUILD_DIR:-"$ROOT/build-fork"}

HORIZON_FORK=ON \
BUILD_TESTING=ON \
BUILD_DIR="$BUILD_DIR" \
    "$ROOT/build-m1.sh"

cmake --build "$BUILD_DIR" --target LauncherTests -j "$(sysctl -n hw.ncpu)"

echo
echo "Tests:  $BUILD_DIR/LauncherTests"
echo "App:    $BUILD_DIR/horizon-prism.app"
echo "Binary: $BUILD_DIR/horizon-prism.app/Contents/MacOS/horizon-prism"
