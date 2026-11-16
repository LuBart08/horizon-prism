#!/bin/sh
# Create a throwaway Prism data dir with one Horizon instance (M1 test fixture).
#
# Usage: ./make-test-instance.sh [DATA_DIR]
# Then:  PrismLauncher -d DATA_DIR -l HorizonTest -o TestPlayer
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DATA_DIR=${1:-"$ROOT/testdata"}
INST="$DATA_DIR/instances/HorizonTest"

mkdir -p "$INST/horizon/resources" "$INST/horizon/home" "$INST/horizon/root"

cat > "$INST/instance.cfg" <<'EOF'
InstanceType=Horizon
name=Horizon Test
iconKey=default
EOF

cat > "$INST/horizon.json" <<'EOF'
{
  "formatVersion": 1,
  "channel": "stable",
  "minecraft": "26.3",
  "clientVersion": "nightly-2026-11-13-9f42064",
  "preparerVersion": "nightly-2026-11-13-9f42064",
  "pins": { "launch_api": "0.1.0", "output_version": 4, "native_hooks_api": 415 }
}
EOF

echo "Horizon test instance: $INST"
