#!/usr/bin/env bash
set -euo pipefail

TARGET="$HOME/.gemini/config/plugins/antigravity-clone"
REPO="https://github.com/revolverocelot1/antigravity-clone.git"

echo "Installing antigravity-clone to $TARGET..."

if [ -d "$TARGET" ]; then
    if [ -d "$TARGET/.git" ]; then
        echo "Updating existing installation via git pull..."
        git -C "$TARGET" pull --ff-only
    else
        echo "Target directory exists without git."
    fi
else
    mkdir -p "$(dirname "$TARGET")"
    git clone "$REPO" "$TARGET"
fi

echo "Verifying installation..."
python3 -u "$TARGET/test_clone_plugin.py"

echo ""
echo "Antigravity Clone installed successfully! Type /clone or .clone in any chat."
