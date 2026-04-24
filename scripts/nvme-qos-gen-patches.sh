#!/usr/bin/env bash
#
# gen-patches.sh - Generate the distributable NVMe QoS driver patch.
#
# The patch covers only drivers/nvme/host/ and is meant to be applied
# against the upstream base commit with:
#
#   git apply patches/nvme-qos.patch
#   patch -p1 < patches/nvme-qos.patch
#
# Update BASE_COMMIT when rebasing onto a new upstream kernel.
#
# Note: patches/ is ignored by the kernel's .gitignore (quilt conflict).
# Stage generated files with: git add -f patches/

set -euo pipefail

# Upstream base: "Merge branch 'torvalds:master' into master" (Linux 6.18.0-rc2, 2025-10-24).
# This is the last clean upstream state before any QoS code was introduced.
BASE_COMMIT="571177ce418c"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PATCHES_DIR="$REPO_ROOT/patches"
OUTPUT="$PATCHES_DIR/nvme-qos.patch"

mkdir -p "$PATCHES_DIR"
git -C "$REPO_ROOT" diff "$BASE_COMMIT"..HEAD -- drivers/nvme/host/ > "$OUTPUT"

echo "Generated: $OUTPUT"
echo "Base commit: $BASE_COMMIT"
echo "Lines: $(wc -l < "$OUTPUT")"
