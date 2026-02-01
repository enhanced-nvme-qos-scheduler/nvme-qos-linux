#!/bin/bash
#
# nvme-qos-build.sh - NVMe QoS Development Kernel Build Script
#
# A comprehensive build management tool for the NVMe QoS scheduler project.
# Features self-managed configuration, smart module reloading, ccache integration,
# and detailed system diagnostics.
#
# Usage: nvme-qos-build <command> [options]
#        nvme-qos-build help <subcommand>
#
# Commands:
#   init      Configure build environment (first-time setup)
#   status    Show comprehensive build and system status
#   build     Build kernel or module
#   install   Install built kernel
#   reload    Smart NVMe module reload
#   clean     Clean build artifacts
#   config    Kernel configuration management
#   boot      GRUB boot management
#   help      Show this help
#

set -euo pipefail

VERSION="0.0.1"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/nvme-qos-build"
CONFIG_FILE="$CONFIG_DIR/config"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
NVME_QOS_MAX_BATCH=4

KBUILD_DIR=""
USE_CCACHE=1
DEFAULT_JOBS="auto"
LOG_RETENTION_DAYS=7
VERBOSE=0
YES_MODE=0
QUIET_MODE=0

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

info() {
    [[ "$QUIET_MODE" -eq 1 ]] && return
    echo -e "${GREEN}[INFO]${NC} $*"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

error() {
    echo -e "${RED}[ERROR]${NC} $*" >&2
}

die() {
    error "$@"
    exit 1
}

debug() {
    [[ "$VERBOSE" -eq 1 ]] && echo -e "${CYAN}[DEBUG]${NC} $*"
}

header() {
    [[ "$QUIET_MODE" -eq 1 ]] && return
    echo -e "\n${BOLD}─── $* ───${NC}"
}

box_header() {
    local text="$1"
    local width=64
    local padding=$(( (width - ${#text} - 2) / 2 ))
    echo ""
    echo -e "${BOLD}╔$(printf '═%.0s' $(seq 1 $width))╗${NC}"
    printf "${BOLD}║%*s%s%*s║${NC}\n" $padding "" "$text" $((width - padding - ${#text})) ""
    echo -e "${BOLD}╚$(printf '═%.0s' $(seq 1 $width))╝${NC}"
}

success() {
    echo -e "${GREEN}[SUCCESS]${NC} $*"
}

confirm() {
    [[ "$YES_MODE" -eq 1 ]] && return 0
    local prompt="${1:-Continue?}"
    local default="${2:-n}"
    local yn_hint="[y/N]"
    [[ "$default" == "y" ]] && yn_hint="[Y/n]"

    read -r -p "$prompt $yn_hint: " response
    response="${response:-$default}"
    [[ "$response" =~ ^[Yy]$ ]]
}

declare -g TIMER_START=0
timer_start() {
    TIMER_START=$(date +%s)
}

timer_elapsed() {
    local end
    end=$(date +%s)
    local diff=$((end - TIMER_START))
    printf '%dm %ds' $((diff / 60)) $((diff % 60))
}

timer_elapsed_seconds() {
    local end
    end=$(date +%s)
    echo $((end - TIMER_START))
}

load_config() {
    if [[ -f "$CONFIG_FILE" ]]; then
        # shellcheck source=/dev/null
        source "$CONFIG_FILE"
        debug "Loaded config from $CONFIG_FILE"
        return 0
    fi
    return 1
}

save_config() {
    mkdir -p "$CONFIG_DIR"
    cat > "$CONFIG_FILE" << EOF
# nvme-qos-build configuration
# Created: $(date '+%Y-%m-%d %H:%M:%S')

KBUILD_DIR="$KBUILD_DIR"
USE_CCACHE=$USE_CCACHE
DEFAULT_JOBS="$DEFAULT_JOBS"
LOG_RETENTION_DAYS=$LOG_RETENTION_DAYS
EOF
    info "Configuration saved to $CONFIG_FILE"
}

ensure_config() {
    if ! load_config || [[ -z "$KBUILD_DIR" ]]; then
        echo -e "${YELLOW}[SETUP]${NC} No configuration found. Running initial setup..."
        echo ""
        cmd_init
    fi
}

cmd_init() {
    box_header "NVMe QoS Build Configuration"

    # Load existing config if present
    load_config 2>/dev/null || true

    # Build directory
    local default_kbuild="${KBUILD_DIR:-/home/branp/kbuild/nvme-dev}"
    read -r -p "Build directory path [$default_kbuild]: " input_kbuild
    KBUILD_DIR="${input_kbuild:-$default_kbuild}"

    # Validate/create build directory
    if [[ ! -d "$KBUILD_DIR" ]]; then
        if confirm "Build directory does not exist. Create it?" "y"; then
            mkdir -p "$KBUILD_DIR"
            info "Created $KBUILD_DIR"
        else
            die "Build directory required. Aborting."
        fi
    fi

    # ccache
    if command -v ccache &>/dev/null; then
        local default_ccache="y"
        [[ "$USE_CCACHE" -eq 0 ]] && default_ccache="n"
        if confirm "Enable ccache for faster rebuilds?" "$default_ccache"; then
            USE_CCACHE=1
        else
            USE_CCACHE=0
        fi
    else
        warn "ccache not installed. Install with: sudo apt install ccache"
        USE_CCACHE=0
    fi

    # Parallel jobs
    local cpu_count
    cpu_count=$(nproc)
    local default_jobs="${DEFAULT_JOBS:-auto}"
    read -r -p "Parallel jobs (number or 'auto' for $cpu_count cores) [$default_jobs]: " input_jobs
    DEFAULT_JOBS="${input_jobs:-$default_jobs}"

    # Log retention
    read -r -p "Log retention days [$LOG_RETENTION_DAYS]: " input_retention
    LOG_RETENTION_DAYS="${input_retention:-$LOG_RETENTION_DAYS}"

    # Save configuration
    save_config

    echo ""
    success "Configuration complete!"
    echo ""
    echo "Next steps:"
    echo "  1. Run './scripts/nvme-qos-build.sh status' to check system state"
    echo "  2. Run './scripts/nvme-qos-build.sh config qos enable' to enable QoS"
    echo "  3. Run './scripts/nvme-qos-build.sh build' to build the kernel"
}

get_job_count() {
    if [[ "$DEFAULT_JOBS" == "auto" ]]; then
        nproc
    else
        echo "$DEFAULT_JOBS"
    fi
}

detect_root_on_nvme() {
    local root_dev
    root_dev=$(findmnt -n -o SOURCE /)
    # Resolve symlinks (e.g., /dev/mapper/*)
    root_dev=$(readlink -f "$root_dev" 2>/dev/null || echo "$root_dev")

    if [[ "$root_dev" == /dev/nvme* ]]; then
        echo "yes"
    else
        echo "no"
    fi
}

detect_boot_on_nvme() {
    local boot_dev
    # Check if /boot is a separate mount
    if findmnt /boot &>/dev/null; then
        boot_dev=$(findmnt -n -o SOURCE /boot)
    else
        # /boot is on root partition
        boot_dev=$(findmnt -n -o SOURCE /)
    fi
    boot_dev=$(readlink -f "$boot_dev" 2>/dev/null || echo "$boot_dev")

    if [[ "$boot_dev" == /dev/nvme* ]]; then
        echo "yes"
    else
        echo "no"
    fi
}

list_nvme_mounts() {
    # List NVMe mounts excluding root
    findmnt -l -n -o TARGET,SOURCE,FSTYPE | grep 'nvme' | while read -r target source fstype; do
        # Skip root
        if [[ "$target" != "/" ]]; then
            echo "$target|$source|$fstype"
        fi
    done
}

get_module_state() {
    if [[ -d /sys/module/nvme ]]; then
        if [[ -f /sys/module/nvme/initstate ]]; then
            echo "module"
        else
            echo "builtin"
        fi
    else
        echo "not_loaded"
    fi
}

get_running_kernel() {
    uname -r
}

get_built_kernel() {
    if [[ -f "$KBUILD_DIR/include/config/kernel.release" ]]; then
        cat "$KBUILD_DIR/include/config/kernel.release"
    else
        echo "not_built"
    fi
}

get_installed_kernels() {
    # List installed dev kernels
    ls /boot/vmlinuz-*-nvme-dev* 2>/dev/null | sed 's|/boot/vmlinuz-||' || true
}

check_qos_config() {
    local config_file="$KBUILD_DIR/.config"

    if [[ ! -f "$config_file" ]]; then
        echo "missing"
        return
    fi

    if grep -q "^CONFIG_NVME_QOS=y" "$config_file"; then
        echo "enabled"
    elif grep -q "^# CONFIG_NVME_QOS is not set" "$config_file"; then
        echo "disabled"
    else
        echo "unknown"
    fi
}

check_qos_sysfs() {
    if [[ -f /sys/class/nvme/nvme0/qos_enable ]]; then
        echo "present"
    else
        echo "absent"
    fi
}

enable_qos_config() {
    local config_file="$KBUILD_DIR/.config"

    if [[ ! -f "$config_file" ]]; then
        die "No .config found. Run a full build first or copy a config."
    fi

    info "Enabling CONFIG_NVME_QOS..."
    "$SRC_DIR/scripts/config" --file "$config_file" --enable NVME_QOS

    info "Running oldconfig to update dependencies..."
    (set +o pipefail; yes "" | make -C "$SRC_DIR" O="$KBUILD_DIR" oldconfig >/dev/null) || true

    success "CONFIG_NVME_QOS enabled"
}

disable_qos_config() {
    local config_file="$KBUILD_DIR/.config"

    if [[ ! -f "$config_file" ]]; then
        die "No .config found."
    fi

    info "Disabling CONFIG_NVME_QOS..."
    "$SRC_DIR/scripts/config" --file "$config_file" --disable NVME_QOS

    info "Running oldconfig to update dependencies..."
    (set +o pipefail; yes "" | make -C "$SRC_DIR" O="$KBUILD_DIR" oldconfig >/dev/null) || true

    success "CONFIG_NVME_QOS disabled"
}

config_backup() {
    local config_file="$KBUILD_DIR/.config"
    local backup_dir="$KBUILD_DIR/.config.backups"
    local timestamp
    timestamp=$(date +%Y%m%d_%H%M%S)

    if [[ ! -f "$config_file" ]]; then
        die "No .config to backup"
    fi

    mkdir -p "$backup_dir"
    cp "$config_file" "$backup_dir/config_$timestamp"
    success "Config backed up to $backup_dir/config_$timestamp"

    # List recent backups
    echo ""
    echo "Recent backups:"
    ls -1t "$backup_dir" | head -5 | while read -r f; do
        echo "  $f"
    done
}

config_restore() {
    local backup_dir="$KBUILD_DIR/.config.backups"
    local config_file="$KBUILD_DIR/.config"

    if [[ ! -d "$backup_dir" ]]; then
        die "No backups found in $backup_dir"
    fi

    echo "Available backups:"
    local i=1
    local -a backups
    while IFS= read -r f; do
        backups+=("$f")
        echo "  $i) $f"
        ((i++))
    done < <(ls -1t "$backup_dir")

    if [[ ${#backups[@]} -eq 0 ]]; then
        die "No backups found"
    fi

    read -r -p "Select backup number [1]: " selection
    selection="${selection:-1}"

    local selected="${backups[$((selection-1))]}"
    if [[ -z "$selected" ]]; then
        die "Invalid selection"
    fi

    # Backup current config first
    if [[ -f "$config_file" ]]; then
        cp "$config_file" "$config_file.pre_restore"
        info "Current config saved to .config.pre_restore"
    fi

    cp "$backup_dir/$selected" "$config_file"
    success "Restored $selected"
}

get_qos_sysfs_status() {
    # Get QoS runtime status for all devices
    echo "devices:"
    for dev in /sys/class/nvme/nvme*; do
        [[ -d "$dev" ]] || continue
        local name
        name=$(basename "$dev")
        local qos_enable="N/A"
        local qos_weight="N/A"

        if [[ -f "$dev/qos_enable" ]]; then
            qos_enable=$(cat "$dev/qos_enable")
        fi
        if [[ -f "$dev/qos_weight" ]]; then
            qos_weight=$(cat "$dev/qos_weight")
        fi
        echo "  $name|$qos_enable|$qos_weight"
    done

    echo "namespaces:"
    for ns in /sys/block/nvme*; do
        [[ -d "$ns" ]] || continue
        local name
        name=$(basename "$ns")
        [[ "$name" == nvme*n* ]] || continue

        local qos_policy="N/A"
        if [[ -f "$ns/qos_policy" ]]; then
            qos_policy=$(cat "$ns/qos_policy")
        fi
        echo "  $name|$qos_policy"
    done
}

cmd_config() {
    local subcmd="${1:-show}"
    shift || true

    case "$subcmd" in
        show)
            header "Build Configuration"
            local config_file="$KBUILD_DIR/.config"

            if [[ ! -f "$config_file" ]]; then
                warn "No .config found at $config_file"
                echo "Run 'nvme-qos-build build' to create initial config"
                return
            fi

            local qos_status
            qos_status=$(check_qos_config)

            echo ""
            printf "%-20s %s\n" "Config file:" "$config_file"

            case "$qos_status" in
                enabled)
                    printf "%-20s ${GREEN}ENABLED${NC} (CONFIG_NVME_QOS=y)\n" "NVMe QoS:"
                    ;;
                disabled)
                    printf "%-20s ${YELLOW}DISABLED${NC} (not set)\n" "NVMe QoS:"
                    ;;
                *)
                    printf "%-20s ${RED}%s${NC}\n" "NVMe QoS:" "$qos_status"
                    ;;
            esac

            # Show LOCALVERSION if set
            local localver
            localver=$(grep "^CONFIG_LOCALVERSION=" "$config_file" 2>/dev/null | cut -d'"' -f2 || echo "")
            if [[ -n "$localver" ]]; then
                printf "%-20s %s\n" "LOCALVERSION:" "$localver"
            fi
            ;;

        qos)
            local action="${1:-}"
            case "$action" in
                "")
                    # Just show status
                    local qos_status
                    qos_status=$(check_qos_config)
                    echo ""
                    echo "CONFIG_NVME_QOS status: $qos_status"
                    echo ""
                    echo "Commands:"
                    echo "  ./scripts/nvme-qos-build.sh config qos enable   - Enable QoS"
                    echo "  ./scripts/nvme-qos-build.sh config qos disable  - Disable QoS"
                    ;;
                enable)
                    enable_qos_config
                    ;;
                disable)
                    disable_qos_config
                    ;;
                *)
                    die "Unknown qos action: $action. Use 'enable' or 'disable'."
                    ;;
            esac
            ;;

        menu)
            info "Opening menuconfig..."
            make -C "$SRC_DIR" O="$KBUILD_DIR" menuconfig
            ;;

        backup)
            config_backup
            ;;

        restore)
            config_restore
            ;;

        *)
            die "Unknown config subcommand: $subcmd"
            ;;
    esac
}

cmd_status() {
    box_header "NVMe QoS Build Status"

    # Environment
    header "Environment"
    printf "  %-20s %s\n" "Source directory:" "$SRC_DIR"
    printf "  %-20s %s\n" "Build directory:" "$KBUILD_DIR"
    printf "  %-20s %s\n" "Config file:" "$CONFIG_FILE"

    # ccache status
    if [[ "$USE_CCACHE" -eq 1 ]] && command -v ccache &>/dev/null; then
        local cache_size hit_rate
        cache_size=$(ccache -s 2>/dev/null | grep -E "^Cache size" | awk '{print $3, $4}' || echo "unknown")
        hit_rate=$(ccache -s 2>/dev/null | grep "hit rate" | awk '{print $4}' || echo "unknown")
        printf "  %-20s ${GREEN}enabled${NC} (%s, %s hit rate)\n" "ccache:" "$cache_size" "$hit_rate"
    else
        printf "  %-20s ${DIM}disabled${NC}\n" "ccache:"
    fi

    # Kernel versions
    header "Kernel Versions"
    local running built
    running=$(get_running_kernel)
    built=$(get_built_kernel)
    printf "  %-20s %s\n" "Running kernel:" "$running"
    printf "  %-20s %s\n" "Built kernel:" "$built"

    local installed
    installed=$(get_installed_kernels)
    if [[ -n "$installed" ]]; then
        printf "  %-20s " "Installed dev:"
        echo "$installed" | tr '\n' ', ' | sed 's/,$//'
        echo ""
    fi

    # Build configuration
    header "Build Configuration"
    local qos_status
    qos_status=$(check_qos_config)
    case "$qos_status" in
        enabled)
            printf "  %-20s ${GREEN}ENABLED${NC} (y)\n" "CONFIG_NVME_QOS:"
            ;;
        disabled)
            printf "  %-20s ${YELLOW}DISABLED${NC} (not set)\n" "CONFIG_NVME_QOS:"
            ;;
        missing)
            printf "  %-20s ${DIM}no .config found${NC}\n" "CONFIG_NVME_QOS:"
            ;;
        *)
            printf "  %-20s ${RED}%s${NC}\n" "CONFIG_NVME_QOS:" "$qos_status"
            ;;
    esac

    header "QoS Runtime Status"
    local module_state qos_sysfs
    module_state=$(get_module_state)
    qos_sysfs=$(check_qos_sysfs)

    printf "  %-20s %s\n" "Module state:" "$module_state"
    printf "  %-20s " "QoS sysfs:"
    if [[ "$qos_sysfs" == "present" ]]; then
        echo -e "${GREEN}present${NC} (kernel built with QoS)"
    else
        echo -e "${DIM}absent${NC}"
    fi

    # Show device QoS status if sysfs present
    if [[ "$qos_sysfs" == "present" ]]; then
        echo ""
        printf "  ${BOLD}%-16s %-12s %-12s${NC}\n" "Device" "qos_enable" "qos_weight"
        printf "  %-16s %-12s %-12s\n" "────────────────" "────────────" "────────────"
        for dev in /sys/class/nvme/nvme*; do
            [[ -d "$dev" ]] || continue
            local name qos_enable qos_weight
            name=$(basename "$dev")
            qos_enable=$(cat "$dev/qos_enable" 2>/dev/null || echo "N/A")
            qos_weight=$(cat "$dev/qos_weight" 2>/dev/null || echo "N/A")
            printf "  %-16s %-12s %-12s\n" "$name" "$qos_enable" "$qos_weight"
        done

        echo ""
        printf "  ${BOLD}%-16s %-16s${NC}\n" "Namespace" "qos_policy"
        printf "  %-16s %-16s\n" "────────────────" "────────────────"
        for ns in /sys/block/nvme*; do
            [[ -d "$ns" ]] || continue
            local name qos_policy
            name=$(basename "$ns")
            [[ "$name" == nvme*n* ]] || continue
            qos_policy=$(cat "$ns/qos_policy" 2>/dev/null || echo "N/A")
            printf "  %-16s %-16s\n" "$name" "$qos_policy"
        done
    fi

    # System State (for reload)
    header "System State (for reload)"
    local root_nvme boot_nvme
    root_nvme=$(detect_root_on_nvme)
    boot_nvme=$(detect_boot_on_nvme)

    printf "  %-20s " "Root on NVMe:"
    if [[ "$root_nvme" == "yes" ]]; then
        echo -e "${RED}YES${NC} - module reload NOT possible"
    else
        echo -e "${GREEN}no${NC} - safe to reload"
    fi

    printf "  %-20s " "Boot on NVMe:"
    if [[ "$boot_nvme" == "yes" ]]; then
        echo -e "${RED}YES${NC} - module reload NOT possible"
    else
        echo -e "${GREEN}no${NC} - safe to reload"
    fi

    local nvme_mounts
    nvme_mounts=$(list_nvme_mounts)
    if [[ -n "$nvme_mounts" ]]; then
        echo ""
        printf "  ${BOLD}NVMe mounts (will unmount during reload):${NC}\n"
        echo "$nvme_mounts" | while IFS='|' read -r target source fstype; do
            printf "    %-24s %s\n" "$target" "$source"
        done
    else
        printf "  %-20s ${DIM}none${NC}\n" "NVMe mounts:"
    fi

    # Recent logs
    header "Recent Activity"
    local log_dir="$KBUILD_DIR/logs"
    if [[ -d "$log_dir" ]]; then
        local latest_log
        latest_log=$(ls -1t "$log_dir"/build_*.log 2>/dev/null | head -1)
        if [[ -n "$latest_log" ]]; then
            local log_name log_date
            log_name=$(basename "$latest_log")
            # Extract timestamp from filename build_YYYYMMDD_HHMMSS.log
            log_date=$(echo "$log_name" | sed 's/build_\([0-9]*\)_\([0-9]*\)\.log/\1 \2/' | \
                       sed 's/\(....\)\(..\)\(..\) \(..\)\(..\)\(..\)/\1-\2-\3 \4:\5:\6/')
            printf "  %-20s %s\n" "Last build:" "$log_date"
            printf "  %-20s %s\n" "Build log:" "$latest_log"
        fi
    else
        printf "  %-20s ${DIM}no logs found${NC}\n" "Last build:"
    fi

    echo ""
}

check_prerequisites() {
    local missing=()
    local warnings=()

    header "Checking Prerequisites"

    # Required packages
    local packages=(build-essential flex bison libelf-dev libssl-dev bc)
    for pkg in "${packages[@]}"; do
        if dpkg -s "$pkg" &>/dev/null; then
            debug "  $pkg: installed"
        else
            missing+=("$pkg")
        fi
    done

    # Optional: ccache
    if [[ "$USE_CCACHE" -eq 1 ]]; then
        if ! command -v ccache &>/dev/null; then
            warnings+=("ccache requested but not installed")
        fi
    fi

    # Check disk space (need at least 5GB)
    local free_space
    free_space=$(df -BG "$KBUILD_DIR" 2>/dev/null | awk 'NR==2 {print $4}' | tr -d 'G')
    if [[ -n "$free_space" ]] && [[ "$free_space" -lt 5 ]]; then
        warnings+=("Low disk space: ${free_space}GB free (recommend 5GB+)")
    fi

    # Report findings
    if [[ ${#missing[@]} -gt 0 ]]; then
        error "Missing required packages: ${missing[*]}"
        echo "  Install with: sudo apt install ${missing[*]}"
        return 1
    fi

    for w in "${warnings[@]}"; do
        warn "$w"
    done

    # Check for uncommitted changes in nvme driver
    if git -C "$SRC_DIR" status --porcelain drivers/nvme/host/ 2>/dev/null | grep -q .; then
        warn "Uncommitted changes in drivers/nvme/host/"
        if ! confirm "Proceed anyway?"; then
            return 1
        fi
    fi

    info "Prerequisites OK"
    return 0
}

validate_qos_config() {
    local qos_status
    qos_status=$(check_qos_config)

    if [[ "$qos_status" != "enabled" ]]; then
        echo ""
        warn "CONFIG_NVME_QOS is not enabled in kernel config!"
        echo "    QoS scheduling features will NOT be compiled."
        echo ""

        if confirm "Enable CONFIG_NVME_QOS now?" "y"; then
            enable_qos_config
            return 0
        else
            warn "Building without QoS support"
            return 0
        fi
    fi

    info "CONFIG_NVME_QOS: enabled"
}

setup_ccache() {
    if [[ "$USE_CCACHE" -eq 1 ]] && command -v ccache &>/dev/null; then
        export PATH="/usr/lib/ccache:$PATH"
        export CCACHE_DIR="${CCACHE_DIR:-$HOME/.ccache}"

        local cache_size hit_rate
        cache_size=$(ccache -s 2>/dev/null | grep -E "^Cache size" | awk '{print $3, $4}' || echo "unknown")
        hit_rate=$(ccache -s 2>/dev/null | grep "hit rate" | awk '{print $4}' || echo "N/A")
        info "ccache enabled: $cache_size, $hit_rate hit rate"
        return 0
    fi
    return 1
}

ensure_log_dir() {
    local log_dir="$KBUILD_DIR/logs"
    mkdir -p "$log_dir"

    # Clean old logs
    if [[ "$LOG_RETENTION_DAYS" -gt 0 ]]; then
        find "$log_dir" -name "build_*.log" -mtime +"$LOG_RETENTION_DAYS" -delete 2>/dev/null || true
        find "$log_dir" -name "build_*.err" -mtime +"$LOG_RETENTION_DAYS" -delete 2>/dev/null || true
    fi

    echo "$log_dir"
}

show_build_errors() {
    local log_file="$1"
    local error_count=0
    local -a error_lines=()

    # Extract error lines with context
    while IFS= read -r line; do
        error_lines+=("$line")
        ((error_count++))
    done < <(grep -n -E "error:|fatal error:" "$log_file" | head -10)

    if [[ $error_count -eq 0 ]]; then
        return 0
    fi

    echo ""
    error "Build failed with $error_count error(s)"
    echo ""

    local i=1
    for err_line in "${error_lines[@]}"; do
        local line_num file_info
        line_num=$(echo "$err_line" | cut -d: -f1)
        echo -e "${BOLD}──── Error $i ────${NC}"
        echo "$err_line"
        ((i++))
        [[ $i -gt 5 ]] && break
    done

    if [[ $error_count -gt 5 ]]; then
        echo ""
        echo "... and $((error_count - 5)) more errors"
    fi

    echo ""
    echo "Full log: $log_file"
    return 1
}

show_build_summary() {
    local log_file="$1"
    local start_time="$2"
    local success="$3"

    echo ""
    echo "════════════════════════════════════════════════════════════"
    echo " Build Summary"
    echo "════════════════════════════════════════════════════════════"
    printf " %-14s %s\n" "Started:" "$start_time"
    printf " %-14s %s\n" "Finished:" "$(date '+%Y-%m-%d %H:%M:%S')"
    printf " %-14s %s\n" "Duration:" "$(timer_elapsed)"
    printf " %-14s %s\n" "Jobs:" "$(get_job_count) parallel"

    if [[ "$USE_CCACHE" -eq 1 ]] && command -v ccache &>/dev/null; then
        local hit_rate
        hit_rate=$(ccache -s 2>/dev/null | grep "hit rate" | awk '{print $4}' || echo "N/A")
        printf " %-14s enabled (%s hit rate)\n" "ccache:" "$hit_rate"
    fi

    echo ""

    if [[ "$success" == "true" ]]; then
        local kernel_ver
        kernel_ver=$(get_built_kernel)
        printf " ${GREEN}%-14s %s${NC}\n" "Kernel:" "$kernel_ver"

        # Show sizes if available
        if [[ -f "$KBUILD_DIR/arch/x86/boot/bzImage" ]]; then
            local vmlinuz_size
            vmlinuz_size=$(du -h "$KBUILD_DIR/arch/x86/boot/bzImage" | cut -f1)
            printf " %-14s %s\n" "vmlinuz:" "$vmlinuz_size"
        fi
    else
        printf " ${RED}%-14s FAILED${NC}\n" "Status:"
    fi

    echo "════════════════════════════════════════════════════════════"
}

do_build_full() {
    local clean_first=0
    local install_after=0

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --clean-first) clean_first=1; shift ;;
            --install) install_after=1; shift ;;
            *) shift ;;
        esac
    done

    # Ensure build directory
    mkdir -p "$KBUILD_DIR"

    # Check prerequisites
    check_prerequisites || die "Prerequisites check failed"

    # Setup ccache
    setup_ccache || true

    # Clean if requested
    if [[ "$clean_first" -eq 1 ]]; then
        info "Cleaning build directory..."
        make -C "$SRC_DIR" O="$KBUILD_DIR" clean
    fi

    # Prepare logging
    local log_dir timestamp log_file start_time
    log_dir=$(ensure_log_dir)
    timestamp=$(date +%Y%m%d_%H%M%S)
    log_file="$log_dir/build_$timestamp.log"
    start_time=$(date '+%Y-%m-%d %H:%M:%S')

    # Check/create kernel config
    local config_file="$KBUILD_DIR/.config"
    if [[ ! -f "$config_file" ]]; then
        info "No .config found, creating default config..."
        if ! make -C "$SRC_DIR" O="$KBUILD_DIR" defconfig; then
            die "Failed to create default config"
        fi
    fi

    # Update config (use subshell to avoid pipefail issue with 'yes' getting SIGPIPE)
    info "Updating kernel config..."
    if ! (set +o pipefail; yes "" | make -C "$SRC_DIR" O="$KBUILD_DIR" oldconfig >/dev/null); then
        die "Failed to update kernel config. Check $KBUILD_DIR/.config"
    fi

    # Validate QoS config
    validate_qos_config

    # Build
    header "Building Kernel"
    info "Output: $log_file"
    info "This may take a while..."
    echo ""

    timer_start
    local build_success=true
    local jobs
    jobs=$(get_job_count)

    if make -C "$SRC_DIR" O="$KBUILD_DIR" -j"$jobs" 2>&1 | tee "$log_file"; then
        # Check for errors in log even if make returned success
        if grep -qE "error:|fatal error:" "$log_file"; then
            build_success=false
        fi
    else
        build_success=false
    fi

    if [[ "$build_success" == "false" ]]; then
        show_build_errors "$log_file"
        show_build_summary "$log_file" "$start_time" "false"
        return 1
    fi

    show_build_summary "$log_file" "$start_time" "true"

    # Generate compile_commands.json if script exists
    if [[ -x "$SRC_DIR/scripts/clang-tools/gen_compile_commands.py" ]]; then
        info "Generating compile_commands.json..."
        "$SRC_DIR/scripts/clang-tools/gen_compile_commands.py" -d "$KBUILD_DIR" -o "$SRC_DIR/compile_commands.json" 2>/dev/null || true
    fi

    success "Build complete!"

    if [[ "$install_after" -eq 1 ]]; then
        echo ""
        do_install
    else
        echo ""
        echo "Next steps:"
        echo "  ./scripts/nvme-qos-build.sh install    - Install kernel"
        echo "  ./scripts/nvme-qos-build.sh reload     - Reload NVMe module (if module build)"
    fi
}

do_build_module() {
    local running
    running=$(get_running_kernel)

    if [[ "$running" != *"-nvme-dev"* ]]; then
        die "Module-only build requires booting into the dev kernel first.
Current kernel: $running
Expected: *-nvme-dev*

Run './scripts/nvme-qos-build.sh boot set' then reboot, or use 'build' for full build."
    fi

    # Check prerequisites (subset)
    if ! command -v make &>/dev/null; then
        die "make not found. Install build-essential."
    fi

    # Setup ccache
    setup_ccache || true

    # Validate QoS config
    validate_qos_config

    # Prepare logging
    local log_dir timestamp log_file
    log_dir=$(ensure_log_dir)
    timestamp=$(date +%Y%m%d_%H%M%S)
    log_file="$log_dir/build_$timestamp.log"

    header "Building NVMe Module"
    info "Quick rebuild for iterative development"
    echo ""

    timer_start
    local jobs
    jobs=$(get_job_count)

    if ! make -C "$SRC_DIR" O="$KBUILD_DIR" M=drivers/nvme/host -j"$jobs" 2>&1 | tee "$log_file"; then
        show_build_errors "$log_file"
        return 1
    fi

    if grep -qE "error:|fatal error:" "$log_file"; then
        show_build_errors "$log_file"
        return 1
    fi

    success "Module built in $(timer_elapsed)"

    # Copy module
    info "Installing module..."
    sudo cp "$KBUILD_DIR/drivers/nvme/host/nvme.ko" \
        "/lib/modules/$running/kernel/drivers/nvme/host/"

    success "Module installed"
    echo ""
    echo "Next steps:"
    echo "  ./scripts/nvme-qos-build.sh reload    - Reload NVMe module"
    echo ""
    warn "Reloading will briefly disconnect NVMe devices!"
}

do_install() {
    local built_kernel
    built_kernel=$(get_built_kernel)

    if [[ "$built_kernel" == "not_built" ]]; then
        die "No kernel built. Run './scripts/nvme-qos-build.sh build' first."
    fi

    header "Installing Kernel"
    info "Kernel version: $built_kernel"

    # Install modules
    info "Installing modules..."
    sudo make -C "$SRC_DIR" O="$KBUILD_DIR" modules_install

    # Install kernel
    info "Installing kernel..."
    sudo make -C "$SRC_DIR" O="$KBUILD_DIR" install

    # Update initramfs
    info "Updating initramfs..."
    sudo update-initramfs -c -k "$built_kernel"

    # Update GRUB
    info "Updating GRUB..."
    sudo update-grub

    # Set one-time boot
    info "Setting one-time boot to dev kernel..."
    sudo grub-reboot "Advanced options for Ubuntu>Ubuntu, with Linux $built_kernel"

    success "Installation complete!"
    echo ""
    echo "Run 'sudo reboot' to boot into $built_kernel"
    echo "After reboot, verify with: uname -r"
}

cmd_build() {
    local subcmd="${1:-full}"
    shift || true

    case "$subcmd" in
        full|"")
            do_build_full "$@"
            ;;
        module|mod)
            do_build_module "$@"
            ;;
        --help|-h)
            echo "Usage: nvme-qos-build build [subcommand] [options]"
            echo ""
            echo "Subcommands:"
            echo "  full       Full kernel build (default)"
            echo "  module     NVMe module only (requires dev kernel)"
            echo ""
            echo "Options:"
            echo "  --clean-first    Clean before building"
            echo "  --install        Install after successful build"
            ;;
        *)
            # Might be an option for full build
            do_build_full "$subcmd" "$@"
            ;;
    esac
}

find_processes_using_nvme() {
    local mounts="$1"
    local -a pids=()

    while IFS='|' read -r target source fstype; do
        [[ -z "$target" ]] && continue
        # Find processes with files open on this mount
        local mount_pids
        mount_pids=$(lsof +D "$target" 2>/dev/null | awk 'NR>1 {print $2}' | sort -u)
        for pid in $mount_pids; do
            pids+=("$pid")
        done
    done <<< "$mounts"

    # Deduplicate
    printf '%s\n' "${pids[@]}" | sort -u
}

kill_nvme_processes() {
    local pids="$1"

    if [[ -z "$pids" ]]; then
        return 0
    fi

    local count
    count=$(echo "$pids" | wc -l)
    info "Terminating $count process(es) using NVMe..."

    for pid in $pids; do
        local cmd
        cmd=$(ps -p "$pid" -o comm= 2>/dev/null || echo "unknown")
        debug "  Killing $pid ($cmd)"
        sudo kill "$pid" 2>/dev/null || true
    done

    sleep 1

    # Force kill remaining
    for pid in $pids; do
        if ps -p "$pid" &>/dev/null; then
            debug "  Force killing $pid"
            sudo kill -9 "$pid" 2>/dev/null || true
        fi
    done
}

unmount_nvme_filesystems() {
    local mounts="$1"
    local -a unmounted=()

    while IFS='|' read -r target source fstype; do
        [[ -z "$target" ]] && continue
        info "Unmounting $target..."
        if sudo umount "$target"; then
            unmounted+=("$target|$source|$fstype")
        else
            error "Failed to unmount $target"
            return 1
        fi
    done <<< "$mounts"

    # Store for remount
    printf '%s\n' "${unmounted[@]}"
}

remount_nvme_filesystems() {
    local mounts="$1"

    while IFS='|' read -r target source fstype; do
        [[ -z "$target" ]] && continue
        info "Remounting $target..."
        if ! sudo mount "$source" "$target"; then
            error "Failed to remount $target from $source"
            warn "Manual remount required: sudo mount $source $target"
        fi
    done <<< "$mounts"
}

reload_nvme_module() {
    info "Unloading NVMe modules..."
    sudo modprobe -r nvme nvme_core

    info "Loading NVMe modules..."
    sudo modprobe nvme_core
    sudo modprobe nvme

    # Wait for devices to enumerate
    sleep 2
}

cmd_reload() {
    header "NVMe Module Reload"

    # Safety checks
    local root_nvme boot_nvme module_state
    root_nvme=$(detect_root_on_nvme)
    boot_nvme=$(detect_boot_on_nvme)
    module_state=$(get_module_state)

    info "Analyzing system configuration..."

    if [[ "$root_nvme" == "yes" ]]; then
        die "Root filesystem is on NVMe - cannot reload module.
Reboot required to load new module."
    fi

    if [[ "$boot_nvme" == "yes" ]]; then
        die "Boot partition is on NVMe - cannot reload module.
Reboot required to load new module."
    fi

    if [[ "$module_state" == "builtin" ]]; then
        die "NVMe driver is built-in (not a module).
Module reload not possible. Reboot required."
    fi

    if [[ "$module_state" == "not_loaded" ]]; then
        warn "NVMe module not currently loaded"
        info "Loading modules..."
        sudo modprobe nvme_core
        sudo modprobe nvme
        success "Modules loaded"
        return 0
    fi

    # Check for NVMe mounts
    local nvme_mounts
    nvme_mounts=$(list_nvme_mounts)

    if [[ -n "$nvme_mounts" ]]; then
        echo ""
        warn "NVMe filesystems are mounted:"
        echo "$nvme_mounts" | while IFS='|' read -r target source fstype; do
            printf "    %-24s %s\n" "$target" "$source"
        done
        echo ""

        # Find processes using these mounts
        local pids
        pids=$(find_processes_using_nvme "$nvme_mounts")

        if [[ -n "$pids" ]]; then
            warn "Processes using NVMe mounts will be terminated:"
            for pid in $pids; do
                local cmd
                cmd=$(ps -p "$pid" -o pid=,comm=,args= 2>/dev/null || echo "$pid unknown")
                echo "    $cmd"
            done
            echo ""
        fi

        if ! confirm "Proceed with reload? (mounts will be temporarily unavailable)"; then
            info "Reload cancelled"
            return 0
        fi

        # Kill processes
        if [[ -n "$pids" ]]; then
            info "[1/6] Stopping processes using NVMe mounts..."
            kill_nvme_processes "$pids"
        else
            info "[1/6] No processes to stop"
        fi

        info "[2/6] Syncing filesystems..."
        sync

        info "[3/6] Unmounting NVMe filesystems..."
        local unmounted
        unmounted=$(unmount_nvme_filesystems "$nvme_mounts")

        info "[4/6] Unloading NVMe modules..."
        reload_nvme_module
        info "[5/6] NVMe modules reloaded"

        info "[6/6] Remounting filesystems..."
        remount_nvme_filesystems "$unmounted"
    else
        info "No NVMe mounts to handle"

        if ! confirm "Reload NVMe modules now?"; then
            return 0
        fi

        info "[1/2] Syncing filesystems..."
        sync

        info "[2/2] Reloading NVMe modules..."
        reload_nvme_module
    fi

    # Verify
    echo ""
    success "Module reload complete!"

    local qos_sysfs
    qos_sysfs=$(check_qos_sysfs)
    if [[ "$qos_sysfs" == "present" ]]; then
        local qos_enable
        qos_enable=$(cat /sys/class/nvme/nvme0/qos_enable 2>/dev/null || echo "N/A")
        info "QoS status: qos_enable=$qos_enable"
    fi

    # List re-enumerated devices
    info "Devices:"
    ls /sys/class/nvme/ 2>/dev/null | tr '\n' ' '
    echo ""
}

boot_status() {
    header "GRUB Boot Configuration"

    echo ""
    echo "Current GRUB environment:"
    sudo grub-editenv list 2>/dev/null || echo "  (no environment set)"

    echo ""
    echo "Installed dev kernels:"
    local installed
    installed=$(get_installed_kernels)
    if [[ -n "$installed" ]]; then
        echo "$installed" | while read -r k; do
            echo "  $k"
        done
    else
        echo "  (none)"
    fi

    echo ""
    echo "Running kernel: $(uname -r)"
}

boot_set_onetime() {
    local kernel="${1:-}"

    if [[ -z "$kernel" ]]; then
        # Use built kernel
        kernel=$(get_built_kernel)
        if [[ "$kernel" == "not_built" ]]; then
            die "No kernel specified and no kernel built"
        fi
    fi

    info "Setting one-time boot to: $kernel"
    sudo grub-reboot "Advanced options for Ubuntu>Ubuntu, with Linux $kernel"
    success "One-time boot set. Run 'sudo reboot' to boot into $kernel"
}

boot_set_default() {
    local kernel="${1:-}"

    if [[ -z "$kernel" ]]; then
        kernel=$(get_built_kernel)
        if [[ "$kernel" == "not_built" ]]; then
            die "No kernel specified and no kernel built"
        fi
    fi

    warn "This will change the default boot kernel to: $kernel"
    if ! confirm "Continue?"; then
        return 0
    fi

    info "Setting default boot to: $kernel"
    sudo grub-set-default "Advanced options for Ubuntu>Ubuntu, with Linux $kernel"
    success "Default boot set to $kernel"
}

boot_reset() {
    info "Clearing one-time boot setting..."

    # Only clear one-time boot, preserve user's default
    sudo grub-editenv /boot/grub/grubenv unset next_entry 2>/dev/null || true

    success "One-time boot cleared. Next reboot will use your saved default."

    # Show current state
    echo ""
    echo "Current GRUB environment:"
    sudo grub-editenv list 2>/dev/null || echo "  (no environment set)"
}

boot_clear_default() {
    warn "This will reset the default boot entry to the first menu entry (entry 0)"
    if ! confirm "Continue?"; then
        return 0
    fi

    sudo grub-set-default 0
    success "Default boot reset to first menu entry"
}

boot_remove_kernels() {
    local installed
    installed=$(get_installed_kernels)

    if [[ -z "$installed" ]]; then
        info "No dev kernels installed"
        return 0
    fi

    echo "Installed dev kernels:"
    echo "$installed" | while read -r k; do
        echo "  $k"
    done
    echo ""

    warn "This will remove all dev kernel files and modules"
    if ! confirm "Continue?"; then
        return 0
    fi

    for vmlinuz in /boot/vmlinuz-*-nvme-dev*; do
        [[ -f "$vmlinuz" ]] || continue
        local kver
        kver=$(basename "$vmlinuz" | sed 's/vmlinuz-//')
        info "Removing kernel $kver..."
        sudo rm -f "/boot/vmlinuz-$kver"
        sudo rm -f "/boot/initrd.img-$kver"
        sudo rm -f "/boot/System.map-$kver"
        sudo rm -f "/boot/config-$kver"
        sudo rm -rf "/lib/modules/$kver"
    done

    info "Updating GRUB..."
    sudo update-grub

    success "Dev kernels removed"
}

cmd_boot() {
    local subcmd="${1:-status}"
    shift || true

    case "$subcmd" in
        status)
            boot_status
            ;;
        set)
            boot_set_onetime "$@"
            ;;
        default)
            boot_set_default "$@"
            ;;
        reset)
            boot_reset
            ;;
        clear-default)
            boot_clear_default
            ;;
        remove)
            boot_remove_kernels
            ;;
        --help|-h)
            echo "Usage: nvme-qos-build boot <subcommand>"
            echo ""
            echo "Subcommands:"
            echo "  status         Show current GRUB configuration (default)"
            echo "  set [kernel]   Set one-time boot to dev kernel"
            echo "  default [kernel]  Set default boot to dev kernel"
            echo "  reset          Clear one-time boot (preserves default)"
            echo "  clear-default  Reset default to first menu entry"
            echo "  remove         Remove dev kernels from system"
            ;;
        *)
            die "Unknown boot subcommand: $subcmd"
            ;;
    esac
}

cmd_clean() {
    local what="${1:-}"

    case "$what" in
        ""|all)
            warn "This will clean the entire build directory"
            if ! confirm "Continue?"; then
                return 0
            fi
            info "Cleaning build directory..."
            make -C "$SRC_DIR" O="$KBUILD_DIR" clean
            success "Build directory cleaned"
            ;;
        logs)
            local log_dir="$KBUILD_DIR/logs"
            if [[ -d "$log_dir" ]]; then
                local count
                count=$(find "$log_dir" -name "*.log" | wc -l)
                info "Removing $count log files..."
                rm -f "$log_dir"/*.log "$log_dir"/*.err
                success "Logs cleaned"
            else
                info "No logs to clean"
            fi
            ;;
        mrproper)
            warn "This will remove ALL build artifacts including .config"
            if ! confirm "Continue?"; then
                return 0
            fi
            info "Running mrproper..."
            make -C "$SRC_DIR" O="$KBUILD_DIR" mrproper
            success "Build directory completely cleaned"
            ;;
        --help|-h)
            echo "Usage: nvme-qos-build clean [what]"
            echo ""
            echo "What to clean:"
            echo "  all       Clean build artifacts (default)"
            echo "  logs      Clean only log files"
            echo "  mrproper  Complete clean including .config"
            ;;
        *)
            die "Unknown clean target: $what"
            ;;
    esac
}

cmd_help() {
    local topic="${1:-}"

    if [[ -z "$topic" ]]; then
        cat << 'EOF'
nvme-qos-build - NVMe QoS Development Kernel Build Script

Usage: nvme-qos-build <command> [options]

Commands:
  init       Configure build environment (first-time setup)
  status     Show comprehensive build and system status
  build      Build kernel or module
  install    Install built kernel
  reload     Smart NVMe module reload
  clean      Clean build artifacts
  config     Kernel configuration management
  boot       GRUB boot management
  help       Show this help

Common workflows:

  First time setup:
    ./scripts/nvme-qos-build.sh init
    ./scripts/nvme-qos-build.sh config qos enable
    ./scripts/nvme-qos-build.sh build --install

  Iterative development (after booting dev kernel):
    ./scripts/nvme-qos-build.sh build module
    ./scripts/nvme-qos-build.sh reload

  Check system state:
    ./scripts/nvme-qos-build.sh status

Global options:
  -v, --verbose    Verbose output
  -q, --quiet      Minimal output
  -y, --yes        Auto-confirm prompts

Run 'nvme-qos-build help <command>' for command-specific help.
EOF
        return
    fi

    case "$topic" in
        build)
            cmd_build --help
            ;;
        boot)
            cmd_boot --help
            ;;
        clean)
            cmd_clean --help
            ;;
        config)
            echo "Usage: nvme-qos-build config <subcommand>"
            echo ""
            echo "Subcommands:"
            echo "  show      Show current kernel config status (default)"
            echo "  qos       Show/manage CONFIG_NVME_QOS"
            echo "  menu      Open menuconfig"
            echo "  backup    Backup current .config"
            echo "  restore   Restore .config from backup"
            echo ""
            echo "Examples:"
            echo "  nvme-qos-build config qos enable   Enable QoS in kernel config"
            echo "  nvme-qos-build config qos disable  Disable QoS in kernel config"
            echo "  nvme-qos-build config menu         Open interactive menuconfig"
            ;;
        *)
            die "No help available for: $topic"
            ;;
    esac
}

show_usage() {
    echo "Usage: nvme-qos-build <command> [options]"
    echo "Run 'nvme-qos-build help' for more information."
}

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -v|--verbose)
                VERBOSE=1
                shift
                ;;
            -q|--quiet)
                QUIET_MODE=1
                shift
                ;;
            -y|--yes)
                YES_MODE=1
                shift
                ;;
            -h|--help)
                cmd_help
                exit 0
                ;;
            -j)
                DEFAULT_JOBS="$2"
                shift 2
                ;;
            -j*)
                DEFAULT_JOBS="${1#-j}"
                shift
                ;;
            -*)
                die "Unknown option: $1. Run 'nvme-qos-build help' for usage."
                ;;
            *)
                break
                ;;
        esac
    done

    local cmd="${1:-help}"
    shift || true

    # Commands that don't need config
    case "$cmd" in
        init|help|-h|--help)
            ;;
        *)
            ensure_config
            ;;
    esac

    # Dispatch command
    case "$cmd" in
        init)
            cmd_init "$@"
            ;;
        status)
            cmd_status "$@"
            ;;
        build)
            cmd_build "$@"
            ;;
        install)
            do_install "$@"
            ;;
        reload)
            cmd_reload "$@"
            ;;
        clean)
            cmd_clean "$@"
            ;;
        config)
            cmd_config "$@"
            ;;
        boot)
            cmd_boot "$@"
            ;;
        help)
            cmd_help "$@"
            ;;
        *)
            die "Unknown command: $cmd. Run 'nvme-qos-build help' for usage."
            ;;
    esac
}

main "$@"
