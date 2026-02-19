#!/bin/bash

# nvme-qos-check.sh — Quick environment check for NVMe QoS scheduler status.
# Intended for teams sharing this machine to confirm whether the
# custom NVMe QoS kernel/module is active.

INSTALL_NAME="nvme-qos-check"
INSTALL_DIR="/usr/local/bin"

case "${1:-}" in
--install)
	if [ "$(id -u)" -ne 0 ]; then
		echo "Install requires root. Run: sudo $0 --install"
		exit 1
	fi
	cp "$0" "$INSTALL_DIR/$INSTALL_NAME"
	chmod +x "$INSTALL_DIR/$INSTALL_NAME"
	echo "Installed to $INSTALL_DIR/$INSTALL_NAME"
	echo "All users can now run: $INSTALL_NAME"
	exit 0
	;;
--uninstall)
	if [ "$(id -u)" -ne 0 ]; then
		echo "Uninstall requires root. Run: sudo $0 --uninstall"
		exit 1
	fi
	rm -f "$INSTALL_DIR/$INSTALL_NAME"
	echo "Removed $INSTALL_DIR/$INSTALL_NAME"
	exit 0
	;;
--help)
	echo "Usage: $(basename "$0") [OPTION]"
	echo ""
	echo "Check NVMe QoS scheduler status on this machine."
	echo ""
	echo "Options:"
	echo "  --install    Install as '$INSTALL_NAME' in $INSTALL_DIR (requires sudo)"
	echo "  --uninstall  Remove from $INSTALL_DIR (requires sudo)"
	echo "  --help       Show this help"
	echo ""
	echo "With no arguments, displays the current NVMe QoS environment status."
	exit 0
	;;
esac

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
RESET='\033[0m'

section() { printf "\n${BOLD}=== %s ===${RESET}\n" "$1"; }

section "Kernel"

RUNNING=$(uname -r)
VERSION=$(cat /proc/version 2>/dev/null)

printf "  Running kernel : %s\n" "$RUNNING"

if echo "$RUNNING" | grep -q "generic"; then
	printf "  Type           : ${GREEN}Stock/generic Ubuntu kernel${RESET}\n"
else
	printf "  Type           : ${YELLOW}Custom kernel${RESET}\n"
fi

printf "  Build info     : %s\n" "$VERSION"

section "NVMe Driver"

if ! lsmod | grep -qw nvme; then
	printf "  ${RED}nvme module is not loaded${RESET}\n"
else
	MOD_FILE=$(modinfo -F filename nvme 2>/dev/null)
	MOD_VER=$(modinfo -F version nvme 2>/dev/null)
	printf "  Module loaded  : yes\n"
	printf "  Module file    : %s\n" "${MOD_FILE:-unknown}"
	printf "  Module version : %s\n" "${MOD_VER:-unknown}"

	# Check if the module comes from a custom build directory
	if echo "$MOD_FILE" | grep -q "extra\|updates\|kbuild"; then
		printf "  Source         : ${YELLOW}Custom-built module${RESET}\n"
	else
		printf "  Source         : ${GREEN}Distribution-provided module${RESET}\n"
	fi
fi

section "NVMe QoS Status"

QOS_FOUND=0
for ctrl in /sys/class/nvme/nvme*; do
	[ -d "$ctrl" ] || continue
	CTRL_NAME=$(basename "$ctrl")

	if [ -f "$ctrl/qos_enable" ]; then
		QOS_FOUND=1
		ENABLED=$(cat "$ctrl/qos_enable" 2>/dev/null)
		WEIGHT=$(cat "$ctrl/qos_weight" 2>/dev/null)

		if [ "$ENABLED" = "1" ]; then
			printf "  %-8s : ${YELLOW}QoS ENABLED${RESET}  (weight=%s)\n" \
				"$CTRL_NAME" "${WEIGHT:-?}"
		else
			printf "  %-8s : QoS available but ${GREEN}disabled${RESET}\n" \
				"$CTRL_NAME"
		fi
	else
		printf "  %-8s : ${GREEN}No QoS (standard upstream driver)${RESET}\n" \
			"$CTRL_NAME"
	fi
done

if [ "$QOS_FOUND" -eq 0 ]; then
	printf "\n  ${GREEN}No NVMe controllers have QoS support.${RESET}\n"
	printf "  The NVMe driver is behaving identically to upstream.\n"
fi

NS_QOS=0
for ns in /sys/block/nvme*; do
	[ -d "$ns" ] || continue
	if [ -f "$ns/qos_policy" ]; then
		if [ "$NS_QOS" -eq 0 ]; then
			section "Per-Namespace QoS Policies"
			NS_QOS=1
		fi
		POLICY=$(cat "$ns/qos_policy" 2>/dev/null)
		printf "  %-12s : %s\n" "$(basename "$ns")" "${POLICY:-unknown}"
	fi
done

section "Summary"

if [ "$QOS_FOUND" -eq 1 ]; then
	printf "  ${YELLOW}This machine is running the NVMe QoS scheduler module.${RESET}\n"
	printf "  I/O scheduling behavior may differ from a stock kernel.\n"
	printf "  To disable:  echo 0 > /sys/class/nvme/<ctrl>/qos_enable\n"
else
	printf "  ${GREEN}Environment is standard.${RESET} No NVMe QoS modifications are active.\n"
	printf "  I/O behavior is identical to upstream Linux.\n"
fi

printf "\n"
