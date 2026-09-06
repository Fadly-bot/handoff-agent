#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$HOME/.handoff"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$INSTALL_DIR/bin/handoff"
SYMLINK="$BIN_DIR/handoff"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[handoff]${NC} $*"; }
warn()  { echo -e "${YELLOW}[handoff]${NC} $*"; }
error() { echo -e "${RED}[handoff]${NC} $*" >&2; }
die()   { error "$*"; exit 1; }

find_python() {
    local candidates=("python3.13" "python3.12" "python3.11" "python3")
    for candidate in "${candidates[@]}"; do
        if command -v "$candidate" >/dev/null 2>&1; then
            local version
            version=$("$candidate" --version 2>&1 | awk '{print $2}')
            local major minor
            major=$(echo "$version" | cut -d. -f1)
            minor=$(echo "$version" | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

info "Installing Handoff Agent..."

info "Checking Python..."
PYTHON=$(find_python) || die "Python 3.11+ not found. Please install Python 3.11 or newer."
info "Using: $PYTHON ($($PYTHON --version 2>&1))"

info "Creating directories..."
mkdir -p "$INSTALL_DIR"/{bin,src,cache}
mkdir -p "$BIN_DIR"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

copy_source() {
    # Use rsync if available (fast, preserves perms), else fall back to cp -r.
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
            --exclude='.git' \
            --exclude='node_modules' \
            --exclude='__pycache__' \
            --exclude='.pytest_cache' \
            --exclude='*.pyc' \
            --exclude='venv' \
            --exclude='.venv' \
            "$SCRIPT_DIR/src/" "$INSTALL_DIR/src/"
    else
        rm -rf "$INSTALL_DIR/src"
        mkdir -p "$INSTALL_DIR/src"
        # find+cp_r: standard Unix fallback, skips unwanted dirs/files.
        find "$SCRIPT_DIR/src" \
            -name '.git' -prune -o \
            -name 'node_modules' -prune -o \
            -name '__pycache__' -prune -o \
            -name '.pytest_cache' -prune -o \
            -name 'venv' -prune -o \
            -name '.venv' -prune -o \
            -name '*.pyc' -prune -o \
            ! -type d -print0 2>/dev/null \
            | while IFS= read -r -d '' f; do
                rel="${f#"$SCRIPT_DIR/src/"}"
                mkdir -p "$INSTALL_DIR/src/$(dirname "$rel")"
                cp "$f" "$INSTALL_DIR/src/$rel"
            done
    fi
}

info "Copying source files..."
copy_source

if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
fi

info "Setting up Python virtual environment..."
if [ ! -d "$INSTALL_DIR/venv" ]; then
    "$PYTHON" -m venv "$INSTALL_DIR/venv"
fi

# NOTE: the runtime depends only on the Python standard library, so there are
# no runtime dependencies to install. requirements.txt only pins dev/test tooling.

info "Creating launcher script..."
cp "$SCRIPT_DIR/bin/handoff" "$LAUNCHER"
chmod +x "$LAUNCHER"

info "Creating symlink..."
ln -sf "$LAUNCHER" "$SYMLINK"

info "Creating default config..."
CONFIG_FILE="$INSTALL_DIR/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    cat > "$CONFIG_FILE" << 'CONFIG_EOF'
{
  "version": "0.1.0",
  "default_provider": "claude",
  "output": "docs/HANDOFF.md",
  "auto_commit": false,
  "auto_push": false,
  "providers": {
    "claude": {
      "api_key_env": "ANTHROPIC_API_KEY",
      "model": ""
    },
    "openai": {
      "api_key_env": "OPENAI_API_KEY",
      "model": ""
    },
    "qwen": {
      "api_key_env": "DASHSCOPE_API_KEY",
      "model": ""
    },
    "deepseek": {
      "api_key_env": "DEEPSEEK_API_KEY",
      "model": ""
    }
  }
}
CONFIG_EOF
    info "Config created at: $CONFIG_FILE"
else
    info "Config already exists, skipping."
fi

info "Verifying installation..."
if "$SYMLINK" --version >/dev/null 2>&1; then
    VERSION=$("$SYMLINK" --version 2>&1)
    info "Version: $VERSION"
else
    warn "Could not verify handoff --version, but installation completed."
fi

echo ""
info "Installation complete!"
echo ""
echo "  Installed to:  $INSTALL_DIR"
echo "  Command:       $SYMLINK"
echo "  Config:        $CONFIG_FILE"
echo ""
echo "  Quick start:"
echo "    handoff --help"
echo "    handoff --version"
echo "    handoff --dry-run"
echo ""

PATH_SHOULD_BE=true
if echo "$PATH" | tr ':' '\n' | grep -q "^$BIN_DIR$"; then
    PATH_SHOULD_BE=false
fi

if [ "$PATH_SHOULD_BE" = true ]; then
    warn "$BIN_DIR is not in your PATH."
    echo "  Add this to your shell profile (~/.bashrc or ~/.zshrc):"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
fi
