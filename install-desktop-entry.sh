#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$HOME/.local/share/applications/proton-launcher.desktop"
mkdir -p "$(dirname "$TARGET")"
EXEC_PATH="$ROOT/run.sh"
ICON_PATH="$ROOT/assets/proton-launcher.svg"
# Escape characters that are special in both a desktop Exec quoted argument
# and the sed replacement string. Spaces are handled by the template quotes.
ESCAPED_EXEC=${EXEC_PATH//\\/\\\\}
ESCAPED_EXEC=${ESCAPED_EXEC//\"/\\\"}
# sed consumes one escaping layer in its replacement string. Preserve a
# backslash for the desktop-entry parser in front of both reserved characters.
ESCAPED_EXEC=${ESCAPED_EXEC//\$/\\\\$}
ESCAPED_EXEC=${ESCAPED_EXEC//\`/\\\\\`}
ESCAPED_EXEC=${ESCAPED_EXEC//&/\\&}
ESCAPED_EXEC=${ESCAPED_EXEC//|/\\|}
ESCAPED_ICON=${ICON_PATH//\\/\\\\}
ESCAPED_ICON=${ESCAPED_ICON//&/\\&}
ESCAPED_ICON=${ESCAPED_ICON//|/\\|}
sed \
    -e "s|@EXEC@|$ESCAPED_EXEC|g" \
    -e "s|@ICON@|$ESCAPED_ICON|g" \
    "$ROOT/proton-launcher.desktop.in" > "$TARGET"
chmod +x "$TARGET"
if command -v desktop-file-validate >/dev/null; then
    desktop-file-validate "$TARGET"
fi
command -v update-desktop-database >/dev/null && update-desktop-database "$(dirname "$TARGET")" || true
echo "Installed $TARGET"
