#!/bin/sh
# LlamaNet macOS/Linux Uninstaller
# Usage: curl -sSL https://llamanet.app/uninstall.sh | sh
set -e

LLAMANET_HOME="${HOME}/.llamanet"
BIN_DIR="${HOME}/.local/bin"
LAUNCH_SCRIPT="${BIN_DIR}/llamanet"
DESKTOP_SHORTCUT="${HOME}/Desktop/LlamaNet.command"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

printf "${YELLOW}%s${NC}\n" "  LlamaNet Uninstaller"
printf "%s\n" "  ─────────────────────────────────"
echo ""

printf "  This will remove all LlamaNet files. Continue? [y/N]: "
read -r REPLY
case "$REPLY" in
    [yY]) ;;
    *) echo "  Cancelled."; exit 0 ;;
esac

if [ -d "$LLAMANET_HOME" ]; then
    printf "  Removing %s...\n" "$LLAMANET_HOME"
    rm -rf "$LLAMANET_HOME"
    printf "${GREEN}%s${NC}\n" "  Removed install directory"
fi

if [ -f "$LAUNCH_SCRIPT" ]; then
    rm -f "$LAUNCH_SCRIPT"
    printf "${GREEN}%s${NC}\n" "  Removed CLI launcher"
fi

if [ -f "$DESKTOP_SHORTCUT" ]; then
    rm -f "$DESKTOP_SHORTCUT"
    printf "${GREEN}%s${NC}\n" "  Removed Desktop shortcut"
fi

for SHELL_RC in "${HOME}/.zshrc" "${HOME}/.bashrc" "${HOME}/.profile"; do
    if [ -f "$SHELL_RC" ]; then
        if grep -q '# LlamaNet' "$SHELL_RC" 2>/dev/null; then
            sed -i.bak '/# LlamaNet/,/^$/d' "$SHELL_RC" 2>/dev/null || true
            rm -f "${SHELL_RC}.bak"
            printf "${GREEN}%s${NC}\n" "  Removed PATH entry from $SHELL_RC"
        fi
    fi
done

echo ""
printf "%s\n" "  ─────────────────────────────────"
printf "${GREEN}%s${NC}\n" "  LlamaNet uninstalled successfully."
echo ""
echo "  Note: Model files at ~/.cache/huggingface/ were not removed."
echo "  Remove manually if desired: rm -rf ~/.cache/huggingface"
echo ""
