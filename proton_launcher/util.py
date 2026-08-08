# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import os
from pathlib import Path


def expanded_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve(
        strict=False
    )


def unquote_path(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value
