#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$ROOT/helpers/steam-retry-helper.c"
OUTPUT="$ROOT/helpers/steam-retry-helper.exe"
OBJECT="$(mktemp "${TMPDIR:-/tmp}/steam-retry-helper.XXXXXX.obj")"
trap 'rm -f -- "$OBJECT"' EXIT

command -v clang >/dev/null || {
    echo "clang is required to build the Steam retry helper" >&2
    exit 1
}
command -v lld-link >/dev/null || {
    echo "lld-link is required to build the Steam retry helper" >&2
    exit 1
}

WINE_INCLUDE=""
for directory in /usr/include/wine/windows /usr/include/wine/wine/windows; do
    if [[ -f "$directory/windows.h" ]]; then
        WINE_INCLUDE="$directory"
        break
    fi
done
[[ -n "$WINE_INCLUDE" ]] || {
    echo "Wine development headers are required to build the Steam retry helper" >&2
    exit 1
}

WINE_LIBRARY=""
for directory in \
    /usr/lib/wine/x86_64-windows \
    /usr/lib/x86_64-linux-gnu/wine/x86_64-windows; do
    if [[ -f "$directory/libkernel32.a" ]]; then
        WINE_LIBRARY="$directory"
        break
    fi
done
[[ -n "$WINE_LIBRARY" ]] || {
    echo "Wine's x86-64 import libraries are required to build the Steam retry helper" >&2
    exit 1
}

MULTIARCH="$(cc -print-multiarch 2>/dev/null || true)"
SYSTEM_INCLUDES=(-isystem /usr/include)
if [[ -n "$MULTIARCH" && -d "/usr/include/$MULTIARCH" ]]; then
    SYSTEM_INCLUDES+=(-isystem "/usr/include/$MULTIARCH")
fi

clang \
    --target=x86_64-w64-windows-gnu \
    -I"$WINE_INCLUDE" \
    "${SYSTEM_INCLUDES[@]}" \
    -ffreestanding \
    -fno-stack-protector \
    -fno-builtin \
    -O2 \
    -Wall \
    -Wextra \
    -c "$SOURCE" \
    -o "$OBJECT"
lld-link \
    /entry:mainCRTStartup \
    /subsystem:console \
    /nodefaultlib \
    /timestamp:0 \
    /machine:x64 \
    "/libpath:$WINE_LIBRARY" \
    "$OBJECT" \
    "/out:$OUTPUT" \
    libkernel32.a
