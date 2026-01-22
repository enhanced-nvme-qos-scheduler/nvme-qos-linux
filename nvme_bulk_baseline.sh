#!/usr/bin/env bash
set -euo pipefail

DEFAULT_TARGET="/dev/disk/by-id/nvme-eui.0025385a51a07cf9-part7"
SYMLINK="/dev/nvme_scratch"
MOUNTPOINT="/mnt/nvme_scratch"
MODE="fs"
FORCE_DEVICE=0
USER_TARGET=""

print_usage() {
  cat <<'EOF'
Usage: ./nvme_bulk_baseline.sh [--raw] [--force-device] [target_device]

Examples:
  ./nvme_bulk_baseline.sh
  ./nvme_bulk_baseline.sh /dev/disk/by-id/...-part7
  ./nvme_bulk_baseline.sh --raw
  ./nvme_bulk_baseline.sh --raw --force-device /dev/nvme0n1p7
EOF
}

warn() {
  echo "[WARN] $*" >&2
}

info() {
  echo "[INFO] $*"
}

die() {
  echo "[ERROR] $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      print_usage
      exit 0
      ;;
    --raw)
      MODE="raw"
      shift
      ;;
    --force-device)
      FORCE_DEVICE=1
      shift
      ;;
    --*)
      die "Unknown option: $1"
      ;;
    *)
      if [[ -n "$USER_TARGET" ]]; then
        die "Unexpected extra argument: $1"
      fi
      USER_TARGET="$1"
      shift
      ;;
  esac
done

TARGET="${USER_TARGET:-$DEFAULT_TARGET}"

echo "============================================================"
echo "WARNING: This script performs DESTRUCTIVE fio tests"
echo "FS mode overwrites bulkfile and consumes free space."
echo "Raw mode overwrites the entire target partition."
echo "============================================================"

if [[ $EUID -ne 0 ]]; then
  die "Run as root (required for raw device and mount operations)."
fi

if [[ ! -b "$TARGET" ]]; then
  die "Target is not a block device: $TARGET"
fi

TARGET_REAL="$(readlink -f "$TARGET")"
ROOT_SRC="$(findmnt -n -o SOURCE /)"
ROOT_REAL="$(readlink -f "$ROOT_SRC")"

if [[ "$TARGET_REAL" == "$ROOT_REAL" ]]; then
  die "Target resolves to root device: $TARGET_REAL"
fi

EXPECTED_REAL="/dev/nvme0n1p7"
if [[ $FORCE_DEVICE -ne 1 && "$TARGET_REAL" != "$EXPECTED_REAL" ]]; then
  die "Target must resolve to $EXPECTED_REAL (use --force-device to override)."
fi

info "Using target: $TARGET_REAL"

if [[ $FORCE_DEVICE -ne 1 ]]; then
  target_bytes="$(blockdev --getsize64 "$TARGET_REAL")"
  if (( target_bytes < 40 * 1024 * 1024 * 1024 || target_bytes > 55 * 1024 * 1024 * 1024 )); then
    die "Unexpected target size ($target_bytes bytes). Refusing."
  fi
fi

ln -sfn "$TARGET" "$SYMLINK"
info "Symlink updated: $SYMLINK -> $TARGET"

RESULTS_DIR="./results_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULTS_DIR"
RESULTS_OWNER_UID="${SUDO_UID:-}"
RESULTS_OWNER_GID="${SUDO_GID:-}"
fix_results_owner() {
  if [[ -n "$RESULTS_OWNER_UID" && -n "$RESULTS_OWNER_GID" && -d "$RESULTS_DIR" ]]; then
    chown -R "$RESULTS_OWNER_UID:$RESULTS_OWNER_GID" "$RESULTS_DIR"
  fi
}
trap fix_results_owner EXIT
if [[ -n "$RESULTS_OWNER_UID" && -n "$RESULTS_OWNER_GID" ]]; then
  chown "$RESULTS_OWNER_UID:$RESULTS_OWNER_GID" "$RESULTS_DIR"
fi

info "Writing system info"
uname -a >"$RESULTS_DIR/uname.txt"
cat /proc/cmdline >"$RESULTS_DIR/proc_cmdline.txt"
cat /sys/block/nvme0n1/queue/scheduler >"$RESULTS_DIR/nvme0n1_scheduler.txt"

if command -v nvme >/dev/null 2>&1; then
  nvme list >"$RESULTS_DIR/nvme_list.txt" || warn "nvme list failed"
  ns="${TARGET_REAL%p*}"
  ctrl="${ns%n*}"
  if [[ -n "$ctrl" && -e "$ctrl" ]]; then
    nvme id-ctrl "$ctrl" >"$RESULTS_DIR/nvme_id_ctrl.txt" || warn "nvme id-ctrl failed"
  else
    warn "Unable to derive controller from $TARGET_REAL; skipping nvme id-ctrl"
  fi
else
  warn "nvme-cli not installed; skipping nvme list/id-ctrl"
fi

IOENGINE_LOG="$RESULTS_DIR/ioengine_used.txt"
touch "$IOENGINE_LOG"

run_fio() {
  local name="$1"
  local rw="$2"
  local filename="$3"
  local size="$4"
  local json_out="$5"
  local dmesg_out="$6"

  local common=(
    --name="$name"
    --rw="$rw"
    --bs=1M
    --iodepth=32
    --numjobs=1
    --direct=1
    --group_reporting
    --time_based=0
    --ioengine=io_uring
    --filename="$filename"
    --size="$size"
    --output-format=json
    --output="$json_out"
  )

  if [[ "$MODE" == "raw" ]]; then
    common+=( --hipri=1 )
  fi

  info "Starting fio $name ($rw) with io_uring"
  before_lines="$(dmesg | wc -l)"
  start_line=$((before_lines + 1))
  set +e
  fio "${common[@]}"
  rc=$?
  set -e
  if [[ $rc -eq 0 ]]; then
    echo "$name: io_uring" >>"$IOENGINE_LOG"
  else
    warn "io_uring failed for $name; falling back to libaio"
    local fallback=(
      --name="$name"
      --rw="$rw"
      --bs=1M
      --iodepth=32
      --numjobs=1
      --direct=1
      --group_reporting
      --time_based=0
      --ioengine=libaio
      --filename="$filename"
      --size="$size"
      --output-format=json
      --output="$json_out"
    )
    set +e
    fio "${fallback[@]}"
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
      die "fio fallback failed for $name"
    fi
    echo "$name: libaio" >>"$IOENGINE_LOG"
  fi

  dmesg -T | tail -n +"$start_line" | grep -Ei 'nvme|reset|timeout|abort|I/O error|blk' >"$dmesg_out" || true
}

ensure_fs_mode_mount() {
  if [[ ! -d "$MOUNTPOINT" ]]; then
    mkdir -p "$MOUNTPOINT"
  fi

  if mountpoint -q "$MOUNTPOINT"; then
    local src
    src="$(findmnt -n -o SOURCE "$MOUNTPOINT")"
    local src_real
    src_real="$(readlink -f "$src")"
    if [[ "$src_real" != "$TARGET_REAL" ]]; then
      die "Mountpoint $MOUNTPOINT is not mounted from target device."
    fi
  else
    info "Mounting $SYMLINK to $MOUNTPOINT"
    mount "$SYMLINK" "$MOUNTPOINT"
  fi
}

ensure_raw_mode_unmounted() {
  local mounts
  mounts="$(findmnt -n -o TARGET -S "$TARGET_REAL" || true)"
  if [[ -n "$mounts" ]]; then
    if mountpoint -q "$MOUNTPOINT"; then
      local src
      src="$(findmnt -n -o SOURCE "$MOUNTPOINT")"
      local src_real
      src_real="$(readlink -f "$src")"
      if [[ "$src_real" == "$TARGET_REAL" ]]; then
        info "Unmounting $MOUNTPOINT for raw mode"
        umount "$MOUNTPOINT"
        return
      fi
    fi
    die "Target device is mounted elsewhere; refusing raw test."
  fi
}

if [[ "$MODE" == "fs" ]]; then
  info "Mode: filesystem"
  ensure_fs_mode_mount
  if ! mountpoint -q "$MOUNTPOINT"; then
    die "Failed to mount $MOUNTPOINT"
  fi
  local_src="$(findmnt -n -o SOURCE "$MOUNTPOINT")"
  local_src_real="$(readlink -f "$local_src")"
  if [[ "$local_src_real" != "$TARGET_REAL" ]]; then
    die "Mounted source does not match target device"
  fi

  FILE_PATH="$MOUNTPOINT/bulkfile"
  run_fio "bulk_write" "write" "$FILE_PATH" "40G" \
    "$RESULTS_DIR/fio_bulk_write.json" "$RESULTS_DIR/dmesg_bulk_write.txt"
  run_fio "bulk_read" "read" "$FILE_PATH" "40G" \
    "$RESULTS_DIR/fio_bulk_read.json" "$RESULTS_DIR/dmesg_bulk_read.txt"
else
  info "Mode: raw"
  ensure_raw_mode_unmounted
  run_fio "bulk_write" "write" "$SYMLINK" "100%" \
    "$RESULTS_DIR/fio_bulk_write.json" "$RESULTS_DIR/dmesg_bulk_write.txt"
  run_fio "bulk_read" "read" "$SYMLINK" "100%" \
    "$RESULTS_DIR/fio_bulk_read.json" "$RESULTS_DIR/dmesg_bulk_read.txt"
fi

info "Done. Results saved to $RESULTS_DIR"

if command -v jq >/dev/null 2>&1; then
  info "Summary (fio JSON via jq)"
  for f in "$RESULTS_DIR/fio_bulk_write.json" "$RESULTS_DIR/fio_bulk_read.json"; do
    if [[ -f "$f" ]]; then
      jobname="$(jq -r '.jobs[0].jobname' "$f" 2>/dev/null || echo "fio")"
      rw_dir="$(jq -r 'if .jobs[0].read.io_bytes > 0 then "read" else "write" end' "$f" 2>/dev/null || echo "")"
      bw="$(jq -r ".jobs[0].$rw_dir.bw" "$f" 2>/dev/null || echo "")"
      iops="$(jq -r ".jobs[0].$rw_dir.iops" "$f" 2>/dev/null || echo "")"
      lat="$(jq -r ".jobs[0].$rw_dir.clat_ns.mean" "$f" 2>/dev/null || echo "")"
      if [[ -n "$bw" && "$bw" != "null" ]]; then
        echo "  $jobname: bw=${bw}KB/s iops=$iops clat_mean_ns=$lat"
      else
        echo "  $jobname: see $f"
      fi
    fi
  done
else
  warn "jq not installed; see fio JSON in $RESULTS_DIR"
fi
