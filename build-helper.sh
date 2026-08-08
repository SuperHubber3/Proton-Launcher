#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$ROOT/helpers/runas-helper.c"
OUTPUT="$ROOT/helpers/runas-helper.exe"
MODULE="$ROOT/helpers/runas-helper.exe.so"
if [[ ! -e "$OUTPUT" || ! -e "$MODULE" || "$SOURCE" -nt "$OUTPUT" || "$SOURCE" -nt "$MODULE" ]]; then
    command -v winegcc >/dev/null || {
        echo "winegcc is required to build the Run as administrator helper" >&2
        exit 1
    }
    winegcc -m64 -O2 -o "$OUTPUT" "$SOURCE" -lshell32
fi
