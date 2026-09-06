#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${HANDOFF_HOME:-$HOME/.handoff}"
BIN_DIR="$HOME/.local/bin"
SYMLINK="$BIN_DIR/handoff"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[handoff]${NC} $*"; }
warn()  { echo -e "${YELLOW}[handoff]${NC} $*"; }
error() { echo -e "${RED}[handoff]${NC} $*" >&2; }
die()   { error "$*"; exit 1; }

# Refuse to run against anything that is not a canonical installation path.
case "$INSTALL_DIR" in
    "$HOME"/.handoff) ;&
    "$HOME"/*/.handoff)
        ;;
    *)
        die "Refusing to uninstall: HANDOFF_HOME is not a path we installed into ('$INSTALL_DIR')."
        ;;
esac

if [ ! -e "$SYMLINK" ] && [ ! -e "$INSTALL_DIR" ]; then
    warn "Handoff Agent is not installed. Nothing to do."
    exit 0
fi

info "This will remove:"
[ -e "$SYMLINK" ] && echo "  - $SYMLINK"
[ -e "$INSTALL_DIR" ] && echo "  - $INSTALL_DIR  (source, config, cache, and virtual environment)"
echo ""
read -r -p "Continue? [y/N] " ans
case "$ans" in
    y|Y|yes|YES)
        ;;
    *)
        info "Uninstall cancelled."
        exit 0
        ;;
esac

if [ -L "$SYMLINK" ]; then
    rm -f "$SYMLINK"
    info "Removed symlink: $SYMLINK"
fi

if [ -e "$INSTALL_DIR" ]; then
    rm -rf -- "$INSTALL_DIR"
    info "Removed install directory: $INSTALL_DIR"
fi

echo ""
info "Handoff Agent uninstalled."
if echo "$PATH" | tr ':' '\n' | grep -q "^$BIN_DIR$"; then
    echo "  Note: $BIN_DIR is still in your PATH. You may leave it or remove it manually."
fi