#!/bin/sh
# LlamaNet macOS/Linux Installer
# Usage: curl -sSL https://llamanet.app/install.sh | sh
set -e

LLAMANET_HOME="${HOME}/.llamanet"
VENV_DIR="${LLAMANET_HOME}/venv"
BIN_DIR="${HOME}/.local/bin"
LAUNCH_SCRIPT="${BIN_DIR}/llamanet"
DESKTOP_SHORTCUT="${HOME}/Desktop/LlamaNet.command"
BOOTSTRAP_DEFAULT="https://llamanet.app"
MIN_PYTHON_MINOR=9

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { printf "${BLUE}   %s${NC}\n" "$1"; }
ok()    { printf "${GREEN}   %s${NC}\n" "$1"; }
warn()  { printf "${YELLOW}   %s${NC}\n" "$1"; }
fail()  { printf "${RED}   %s${NC}\n" "$1"; exit 1; }

echo ""
printf "${BLUE}%s${NC}\n" "  LlamaNet Installer"
printf "%s\n" "  ─────────────────────────────────"
echo ""

# ── Step 1: Detect OS ──
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
    Darwin)
        ok "macOS detected ($ARCH)"
        if [ "$ARCH" = "x86_64" ]; then
            ROSETTA=$(/usr/sbin/sysctl -n sysctl.proc_translated 2>/dev/null || echo "0")
            if [ "$ROSETTA" = "1" ]; then
                ok "Apple Silicon (running under Rosetta 2)"
                ARCH="arm64"
            else
                warn "Intel Mac — Metal GPU disabled, CPU-only mode"
            fi
        fi
        ;;
    Linux)
        ok "Linux detected ($ARCH)"
        ;;
    *)
        fail "Unsupported OS: $OS. Use install.ps1 for Windows."
        ;;
esac

# ── Step 2: Find Python ──
find_python() {
    for cmd in python3.11 python3.12 python3.10 python3.9 python3 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            MAJOR=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null)
            MINOR=$("$cmd" -c "import sys; print(sys.version_info.minor)" 2>/dev/null)
            if [ "$MAJOR" = "3" ] && [ "$MINOR" -ge "$MIN_PYTHON_MINOR" ] 2>/dev/null; then
                PYTHON_CMD="$cmd"
                PYTHON_VERSION=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>/dev/null)
                PYTHON_PATH="$(command -v "$cmd")"
                return 0
            fi
        fi
    done
    return 1
}

if find_python; then
    ok "Python $PYTHON_VERSION at $PYTHON_PATH"
else
    warn "Python 3.${MIN_PYTHON_MINOR}+ not found"
    if [ "$OS" = "Darwin" ]; then
        if command -v brew >/dev/null 2>&1; then
            info "Installing Python via Homebrew..."
            brew install python@3.11
        else
            fail "Python not found. Install Homebrew first:\n\n   /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"\n\n   Then: brew install python@3.11"
        fi
    elif [ "$OS" = "Linux" ]; then
        info "Attempting to install Python..."
        if command -v apt-get >/dev/null 2>&1; then
            sudo apt-get update -qq && sudo apt-get install -y -qq python3.11 python3.11-venv python3.11-dev
        elif command -v dnf >/dev/null 2>&1; then
            sudo dnf install -y python3.11 python3.11-devel
        elif command -v pacman >/dev/null 2>&1; then
            sudo pacman -S --noconfirm python
        else
            fail "Cannot install Python automatically. Please install Python 3.9+ manually."
        fi
    fi
    if find_python; then
        ok "Python $PYTHON_VERSION installed"
    else
        fail "Python installation failed. Please install Python 3.${MIN_PYTHON_MINOR}+ manually."
    fi
fi

# ── Step 3: Check Build Tools ──
if [ "$OS" = "Darwin" ]; then
    if xcode-select -p >/dev/null 2>&1; then
        ok "Xcode Command Line Tools installed"
    else
        warn "Xcode Command Line Tools required for native compilation"
        info "Installing Xcode Command Line Tools..."
        xcode-select --install 2>/dev/null
        echo ""
        echo "   A dialog will appear. Click 'Install', wait for completion,"
        echo "   then re-run this installer."
        echo ""
        exit 1
    fi
elif [ "$OS" = "Linux" ]; then
    if command -v gcc >/dev/null 2>&1 || command -v cc >/dev/null 2>&1; then
        ok "C compiler found"
    else
        warn "C compiler not found — llama-cpp-python needs it"
        info "Install with: sudo apt-get install build-essential"
    fi
fi

# ── Step 4: Create Directories ──
mkdir -p "$LLAMANET_HOME"
mkdir -p "$BIN_DIR"
ok "Directories created at $LLAMANET_HOME"

# ── Step 5: Create Virtual Environment ──
UPGRADE_MODE=false

if [ -d "$VENV_DIR" ] && [ -f "$VENV_DIR/bin/activate" ]; then
    if "$VENV_DIR/bin/python" -c "import fastapi" 2>/dev/null; then
        ok "Existing venv found — will upgrade"
        UPGRADE_MODE=true
    else
        warn "Existing venv broken — recreating"
        rm -rf "$VENV_DIR"
    fi
fi

if [ ! -d "$VENV_DIR" ]; then
    info "Creating virtual environment..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
    ok "Virtual environment created"
fi

. "$VENV_DIR/bin/activate"
info "Upgrading pip..."
pip install --upgrade pip setuptools wheel >/dev/null 2>&1

# ── Step 6: Install LlamaNet ──
info "Installing LlamaNet (this may take a few minutes)..."

if [ -f "./pyproject.toml" ] && [ -d "./inference_node" ]; then
    info "Installing from local repository..."
    pip install -e . 2>/dev/null
    pip install -r requirements-inference.txt 2>/dev/null || true
else
    if pip install "llamanet" 2>/dev/null; then
        ok "LlamaNet package installed"
    else
        warn "PyPI install failed — trying direct from GitHub..."
        pip install "git+https://github.com/machaao/llama-net.git"
    fi

    info "Installing inference engine..."
    pip install llama-cpp-python psutil pynvml tqdm huggingface_hub 2>/dev/null || {
        warn "Pre-built wheel not available, building from source..."
        pip install llama-cpp-python
    }
fi

# Verify
if "$VENV_DIR/bin/python" -c "import inference_node" 2>/dev/null; then
    ok "LlamaNet verified"
else
    warn "Package verification failed — attempting repair"
    if [ -f "./pyproject.toml" ]; then
        pip install -e .
    fi
fi

# ── Step 7: Set Default Bootstrap Peers ──
info "Configuring network: joining public LlamaNet network..."
ACTIVATE_SCRIPT="$VENV_DIR/bin/activate"
if ! grep -q 'BOOTSTRAP_PEERS' "$ACTIVATE_SCRIPT" 2>/dev/null; then
    echo '' >> "$ACTIVATE_SCRIPT"
    echo '# LlamaNet: join public network by default' >> "$ACTIVATE_SCRIPT"
    echo "export BOOTSTRAP_PEERS=\"\${BOOTSTRAP_PEERS:-$BOOTSTRAP_DEFAULT}\"" >> "$ACTIVATE_SCRIPT"
fi
export BOOTSTRAP_PEERS="${BOOTSTRAP_PEERS:-$BOOTSTRAP_DEFAULT}"
ok "Bootstrap peers: $BOOTSTRAP_PEERS"

# ── Step 8: Create CLI Launcher ──
info "Creating launcher..."

cat > "$LAUNCH_SCRIPT" << LAUNCHER_EOF
#!/bin/sh
# LlamaNet Launcher
LLAMANET_HOME="\${HOME}/.llamanet"
VENV_DIR="\${LLAMANET_HOME}/venv"

if [ ! -f "\$VENV_DIR/bin/activate" ]; then
    echo "LlamaNet venv not found. Re-run installer:"
    echo "  curl -sSL https://llamanet.app/install.sh | sh"
    exit 1
fi

. "\$VENV_DIR/bin/activate"

# Intel Mac Metal detection
if [ "\$(uname)" = "Darwin" ] && [ "\$(uname -m)" = "x86_64" ]; then
    ROSETTA=\$(/usr/sbin/sysctl -n sysctl.proc_translated 2>/dev/null || echo "0")
    if [ "\$ROSETTA" != "1" ]; then
        export LLAMA_NO_METAL=1
    fi
fi

# Default to public LlamaNet network unless overridden
export BOOTSTRAP_PEERS="\${BOOTSTRAP_PEERS:-$BOOTSTRAP_DEFAULT}"

# Daily update check
UPDATE_CHECK_FILE="\${LLAMANET_HOME}/.last_update_check"
TODAY=\$(date +%Y-%m-%d)
LAST_CHECK=""
if [ -f "\$UPDATE_CHECK_FILE" ]; then
    LAST_CHECK=\$(cat "\$UPDATE_CHECK_FILE")
fi

if [ "\$LAST_CHECK" != "\$TODAY" ]; then
    echo "Checking for updates..."
    pip install --upgrade llamanet 2>/dev/null
    echo "\$TODAY" > "\$UPDATE_CHECK_FILE"
fi

if [ \$# -eq 0 ]; then
    echo ""
    echo "Starting LlamaNet..."
    echo ""

    python -m inference_node.server --host 0.0.0.0 --port 8000 --bootstrap-peers "\$BOOTSTRAP_PEERS" &
    SERVER_PID=\$!

    echo "Waiting for server..."
    READY=false
    for i in \$(seq 1 30); do
        if curl -s http://localhost:8000/health >/dev/null 2>&1; then
            READY=true
            break
        fi
        sleep 1
    done

    if [ "\$READY" = "true" ]; then
        echo ""
        echo "  Web UI:    http://localhost:8000"
        echo "  Network:   Connected to \$BOOTSTRAP_PEERS"
        echo "  API:       http://localhost:8000/v1/chat/completions"
        echo ""
        echo "  Press Ctrl+C to stop"
        echo ""

        if [ "\$(uname)" = "Darwin" ]; then
            open "http://localhost:8000"
        elif command -v xdg-open >/dev/null 2>&1; then
            xdg-open "http://localhost:8000"
        fi

        wait \$SERVER_PID
    else
        echo "Server failed to start. Check logs above."
        kill \$SERVER_PID 2>/dev/null
        exit 1
    fi
else
    python -m inference_node.server "\$@"
fi
LAUNCHER_EOF

chmod +x "$LAUNCH_SCRIPT"
ok "CLI launcher created"

# ── Step 9: Desktop Shortcut ──
if [ "$OS" = "Darwin" ]; then
    cat > "$DESKTOP_SHORTCUT" << DESKTOP_EOF
#!/bin/sh
cd "\$HOME"
exec "\$HOME/.local/bin/llamanet"
DESKTOP_EOF
    chmod +x "$DESKTOP_SHORTCUT"
    ok "Desktop shortcut created"
fi

# ── Step 10: Update PATH ──
SHELL_RC=""
case "$(basename "$SHELL")" in
    zsh)  SHELL_RC="${HOME}/.zshrc" ;;
    bash) SHELL_RC="${HOME}/.bashrc" ;;
    *)    SHELL_RC="${HOME}/.profile" ;;
esac

if echo "$PATH" | grep -qv "$BIN_DIR"; then
    if [ -n "$SHELL_RC" ]; then
        if ! grep -q '.local/bin' "$SHELL_RC" 2>/dev/null; then
            echo '' >> "$SHELL_RC"
            echo '# LlamaNet' >> "$SHELL_RC"
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
            info "Added to PATH in $SHELL_RC"
        fi
    fi
    export PATH="$BIN_DIR:$PATH"
fi

# ── Done ──
echo ""
printf "%s\n" "  ─────────────────────────────────"
echo ""
printf "${GREEN}%s${NC}\n" "  LlamaNet installed successfully!"
echo ""
echo "  Start now:  llamanet"
if [ "$OS" = "Darwin" ]; then
echo "  Or double-click:  ~/Desktop/LlamaNet.command"
fi
echo ""
echo "  The Web UI will open at http://localhost:8000"
echo "  Your node will join the public network at llamanet.app"
echo "  Use the Model Manager to download a GGUF model."
echo ""
echo "  Install path:  $LLAMANET_HOME"
echo "  Python:        $PYTHON_VERSION"
echo "  Network:       $BOOTSTRAP_DEFAULT"
echo ""
printf "%s\n" "  ─────────────────────────────────"
echo ""

if [ -t 0 ]; then
    printf "  Start LlamaNet now? [Y/n]: "
    read -r REPLY
    case "$REPLY" in
        [nN]*) echo "Run 'llamanet' when ready." ;;
        *)     exec "$LAUNCH_SCRIPT" ;;
    esac
fi
