#!/usr/bin/env bash

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
cd "$ROOT"

FAILED=0
FAST=0

while [[ $# -gt 0 ]]; do
	case $1 in
		--fast)
			FAST=1
			shift
			;;
		*)
			echo "Unknown option: $1"
			echo "Usage: $0 [--fast]"
			echo "  --fast    check staged changes only (for pre-commit)"
			echo "  (none)    check changes vs master branch"
			exit 1
			;;
	esac
done

function run_check() {
	local name="$1"
	local cmd="$2"

	local name_len=${#name}
	local dots_len=$((46 - name_len))
	local dots=$(printf '%*s' "$dots_len" | tr ' ' '.')

	printf "%s%s" "$name" "$dots"

	set +e
	log=$(eval "$cmd" 2>&1)
	result=$?
	set -e

	if [[ $result -eq 0 ]]; then
		echo -e "[${GREEN}ok${NC}]"
	else
		echo -e "[${RED}FAIL${NC}]"
		echo "$log"
		FAILED=1
	fi
}

function run_checkpatch() {
	local name="$1"
	local patch="$2"

	local name_len=${#name}
	local dots_len=$((46 - name_len))
	local dots=$(printf '%*s' "$dots_len" | tr ' ' '.')

	printf "%s%s" "$name" "$dots"

	set +e
	log=$(echo "$patch" | ./scripts/checkpatch.pl --no-tree --no-signoff - 2>&1)
	set -e

	local errors
	errors=$(echo "$log" | sed -n 's/.*total: \([0-9][0-9]*\) errors.*/\1/p' | tail -1)
	errors=${errors:-0}

	if [[ "$errors" -eq 0 ]]; then
		echo -e "[${GREEN}ok${NC}]"
	else
		echo -e "[${RED}FAIL${NC}] ($errors errors)"
		echo ""
		echo "$log"
		echo ""
		FAILED=1
	fi
}

ALLOWED_PATHS=(
	'^drivers/nvme/'
	'^\.github/'
	'^README\.md$'
	'^CONTRIBUTING\.md$'
	'^Documentation/images/'
	'^scripts/lint\.sh$'
	'^scripts/install-hooks\.sh$'
	'^scripts/pre-commit$'
	'(^|/)nvme-qos'
)

function check_modified_paths() {
	local failed=0
	local violations=()
	for f in "$@"; do
		local allowed=0
		for pattern in "${ALLOWED_PATHS[@]}"; do
			if [[ "$f" =~ $pattern ]]; then
				allowed=1
				break
			fi
		done
		if [[ $allowed -eq 0 ]]; then
			violations+=("$f")
			failed=1
		fi
	done
	if [[ $failed -eq 1 ]]; then
		echo "Unexpected files modified outside allowed paths:"
		for f in "${violations[@]}"; do
			echo "  $f"
		done
	fi
	return $failed
}

if [[ $FAST -eq 1 ]]; then
	echo "Checking staged changes..."
	echo ""

	STAGED_FILES=$(git diff --cached --name-only --diff-filter=ACMR | grep -E '^drivers/nvme/host/.*\.[ch]$' || true)
	ALL_STAGED=$(git diff --cached --name-only || true)

	if [[ -z "$ALL_STAGED" ]]; then
		echo -e "${GREEN}No staged files to check${NC}"
		exit 0
	fi

	if [[ -n "$STAGED_FILES" ]]; then
		PATCH=$(git diff --cached -- $STAGED_FILES)
		if [[ -n "$PATCH" ]]; then
			run_checkpatch "checkpatch (staged)" "$PATCH"
		fi

		run_check "check_merge_conflicts" "! echo \"$STAGED_FILES\" | xargs -r grep -n -E '^(<{7}|>{7}|={7})'"
	fi

	run_check "check_modified_paths" "check_modified_paths $(echo $ALL_STAGED)"
else
	echo "Checking changes vs master branch (errors only)..."
	echo ""

	if [[ -n "${BASE_SHA:-}" ]]; then
		MERGE_BASE="$BASE_SHA"
	else
		MERGE_BASE=$(git merge-base HEAD master 2>/dev/null || echo "")
	fi
	UNCOMMITTED=$(git diff HEAD -- drivers/nvme/host/)
	if [[ -n "$UNCOMMITTED" ]]; then
		run_checkpatch "checkpatch (uncommitted)" "$UNCOMMITTED"
	else
		echo -e "checkpatch (uncommitted)......................[${GREEN}ok${NC}] (no changes)"
	fi

	if [[ -z "$MERGE_BASE" ]]; then
		echo -e "${YELLOW}Warning: Could not find merge base with master${NC}"
		COMMITTED=""
	else
		COMMITTED=$(git diff "$MERGE_BASE"..HEAD -- drivers/nvme/host/)
	fi

	if [[ -z "$COMMITTED" ]]; then
		echo -e "checkpatch (vs master)........................[${GREEN}ok${NC}] (no changes)"
	else
		run_checkpatch "checkpatch (vs master)" "$COMMITTED"
	fi

	if [[ -n "$MERGE_BASE" ]]; then
		ALL_CHANGED=$(git diff "$MERGE_BASE"..HEAD --name-only || true)
		if [[ -n "$ALL_CHANGED" ]]; then
			run_check "check_modified_paths" "check_modified_paths $(echo $ALL_CHANGED)"
		else
			echo -e "check_modified_paths..........................[${GREEN}ok${NC}] (no changes)"
		fi
	else
		echo -e "${YELLOW}Warning: Skipping path check (no merge base)${NC}"
	fi

	run_check "tabs_not_spaces" "! grep -n '^    ' drivers/nvme/host/pci.c drivers/nvme/host/sysfs.c drivers/nvme/host/nvme.h 2>/dev/null | grep -iE 'qos|high_prio|normal_prio|high_credits|normal_credits'"

	run_check "trailing_whitespace" "! grep -n '[[:space:]]$' drivers/nvme/host/pci.c drivers/nvme/host/sysfs.c drivers/nvme/host/nvme.h 2>/dev/null"

	run_check "check_merge_conflicts" "! grep -rn -E '^(<{7}|>{7}|={7})' drivers/nvme/host/"
fi

if [[ $FAILED -eq 1 ]]; then
	echo ""
	echo -e "${RED}Static analysis failed${NC}"
	exit 1
else
	echo ""
	echo -e "${GREEN}All checks passed${NC}"
fi
