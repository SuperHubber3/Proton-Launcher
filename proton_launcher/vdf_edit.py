# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

VDF_TOKEN = re.compile(rb'//[^\r\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+')


def scalar_value_spans(data: bytes) -> dict[tuple[str, ...], list[tuple[int, int]]]:
    """Return byte spans for scalar VDF values, keyed by their object path."""
    contexts: list[dict[str, Any]] = [{"pending": None, "path": ()}]
    spans: dict[tuple[str, ...], list[tuple[int, int]]] = {}
    for match in VDF_TOKEN.finditer(data):
        token = match.group()
        if token.startswith(b"//"):
            continue
        context = contexts[-1]
        if token == b"{":
            key = context["pending"]
            context["pending"] = None
            if key is None:
                continue
            contexts.append({"pending": None, "path": (*context["path"], key)})
            continue
        if token == b"}":
            if len(contexts) > 1:
                contexts.pop()
            continue

        value = token[1:-1] if token.startswith(b'"') else token
        text = value.decode("utf-8", errors="replace").casefold()
        if context["pending"] is None:
            context["pending"] = text
            continue
        path = (*context["path"], context["pending"])
        start, end = match.span()
        if token.startswith(b'"'):
            start += 1
            end -= 1
        spans.setdefault(path, []).append((start, end))
        context["pending"] = None
    return spans


def replace_scalar_values(
    data: bytes, replacements: Mapping[tuple[str, ...], str]
) -> bytes:
    """Replace existing scalar values without reformatting the VDF document."""
    available = scalar_value_spans(data)
    edits: list[tuple[int, int, bytes]] = []
    for raw_path, value in replacements.items():
        path = tuple(part.casefold() for part in raw_path)
        matches = available.get(path, [])
        if len(matches) != 1:
            raise ValueError(
                f"Expected one direct {' '.join(raw_path)} value, found {len(matches)}"
            )
        start, end = matches[0]
        edits.append((start, end, _encode_token(value)))
    for start, end, value in sorted(edits, reverse=True):
        data = data[:start] + value + data[end:]
    return data


def _encode_token(text: str) -> bytes:
    return text.replace("\\", "\\\\").replace('"', '\\"').encode()


def atomic_write(path: Path, data: bytes) -> None:
    """Replace one file after syncing its temporary copy to disk."""
    path = path.resolve()
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.chmod(path.stat().st_mode)
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    except OSError:
        pass
    finally:
        os.close(directory)


def atomic_write_group(updates: Mapping[Path, bytes]) -> None:
    """Write a validated group of files and roll back if a later write fails."""
    originals = {path: path.read_bytes() for path in updates}
    written: list[Path] = []
    try:
        for path, data in updates.items():
            backup = path.with_name(f"{path.name}.proton-launcher.bak")
            shutil.copy2(path, backup)
            atomic_write(path, data)
            written.append(path)
    except OSError as error:
        failed_rollbacks: list[str] = []
        for path in reversed(written):
            try:
                atomic_write(path, originals[path])
            except OSError as rollback_error:
                failed_rollbacks.append(f"{path}: {rollback_error}")
        if failed_rollbacks:
            raise OSError(
                f"{error}; rolling back earlier writes also failed, leaving "
                "inconsistent files (backups have the .proton-launcher.bak "
                "suffix): " + "; ".join(failed_rollbacks)
            ) from error
        raise
