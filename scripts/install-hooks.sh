#!/usr/bin/env bash

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null && pwd)"
ROOT="$DIR/.."
cd "$ROOT"

echo "Installing git hooks..."
echo ""

# install pre-commit hook
cd .git/hooks
ln -sf ../../scripts/pre-commit pre-commit
cd "$ROOT"
echo "  pre-commit hook installed"

echo ""
echo "Hooks configured:"
echo "  pre-commit:  runs checkpatch.pl on staged changes before commit"
echo ""
echo "To run manually:"
echo "  ./scripts/lint.sh --fast     (staged changes only)"
echo "  ./scripts/lint.sh            (full check on QoS files)"
