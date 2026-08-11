# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import configparser
import json
import os
import re
from pathlib import Path

from .models import GameEntry, GameSource

SUMMARY_URL = "https://www.protondb.com/api/v1/reports/summaries/{app_id}.json"
GAME_URL = "https://www.protondb.com/app/{app_id}"
MAX_APP_ID = 0xFFFFFFFF
METADATA_SEARCH_DEPTH = 5
METADATA_SEARCH_DIRECTORY_LIMIT = 2_000


def summary_url(app_id: int) -> str:
    return SUMMARY_URL.format(app_id=app_id)


def game_url(app_id: int) -> str:
    return GAME_URL.format(app_id=app_id)


def parse_rating(data: bytes) -> str | None:
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    tier = payload.get("tier") if isinstance(payload, dict) else None
    if not isinstance(tier, str) or not tier.strip():
        return None
    return tier.strip().replace("-", " ").title()


def _app_id(value: str) -> int | None:
    value = value.strip().lstrip("\ufeff")
    if not value.isdigit():
        return None
    app_id = int(value)
    return app_id if 0 < app_id <= MAX_APP_ID else None


def _linux_path(value: str) -> Path | None:
    value = os.path.expandvars(os.path.expanduser(value.strip().strip("\"'")))
    if re.match(r"^[zZ]:[\\/]", value):
        value = "/" + value[3:].replace("\\", "/")
    elif re.match(r"^[a-zA-Z]:[\\/]", value):
        return None
    return Path(value).resolve(strict=False) if value else None


def _metadata_file(directory: Path, wanted: str) -> Path | None:
    try:
        return next(
            (
                path
                for path in directory.iterdir()
                if path.name.casefold() == wanted.casefold() and path.is_file()
            ),
            None,
        )
    except OSError:
        return None


def _nested_metadata_files(directory: Path, wanted: str):
    if not directory.is_dir():
        return
    visited = 0
    for current, children, files in os.walk(directory):
        visited += 1
        if visited > METADATA_SEARCH_DIRECTORY_LIMIT:
            return
        depth = len(Path(current).relative_to(directory).parts)
        if depth >= METADATA_SEARCH_DEPTH:
            children.clear()
        children.sort(key=str.casefold)
        for name in sorted(files, key=str.casefold):
            if name.casefold() == wanted.casefold():
                yield Path(current) / name


def _online_fix_app_id(path: Path) -> int | None:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(path.read_text(encoding="utf-8-sig", errors="replace"))
        main = next(
            (section for section in parser.sections() if section.casefold() == "main"),
            "",
        )
        return _app_id(parser.get(main, "RealAppId", fallback="")) if main else None
    except (OSError, configparser.Error):
        return None


def protondb_app_id(game: GameEntry) -> int | None:
    if game.source == GameSource.STEAM:
        return game.app_id

    directories: list[Path] = []
    executable_directory: Path | None = None
    for value, use_parent in (
        (game.shortcut_exe, True),
        (game.shortcut_start_dir, False),
    ):
        path = _linux_path(value)
        if path:
            directory = path.parent if use_parent else path
            if use_parent:
                executable_directory = directory
            if directory not in directories:
                directories.append(directory)

    for directory in directories:
        online_fix = _metadata_file(directory, "OnlineFix.ini")
        if online_fix:
            app_id = _online_fix_app_id(online_fix)
            if app_id:
                return app_id
    if executable_directory:
        for online_fix in _nested_metadata_files(executable_directory, "OnlineFix.ini"):
            app_id = _online_fix_app_id(online_fix)
            if app_id:
                return app_id
    for directory in directories:
        steam_appid = _metadata_file(directory, "steam_appid.txt")
        if steam_appid:
            try:
                app_id = _app_id(
                    steam_appid.read_text(encoding="utf-8-sig", errors="replace")
                )
            except OSError:
                continue
            if app_id:
                return app_id
    if executable_directory:
        for steam_appid in _nested_metadata_files(
            executable_directory, "steam_appid.txt"
        ):
            try:
                app_id = _app_id(
                    steam_appid.read_text(encoding="utf-8-sig", errors="replace")
                )
            except OSError:
                continue
            if app_id:
                return app_id
    return None
