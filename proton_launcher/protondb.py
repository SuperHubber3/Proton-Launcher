# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import configparser
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .models import GameEntry, GameSource

SUMMARY_URL = "https://www.protondb.com/api/v1/reports/summaries/{app_id}.json"
GAME_URL = "https://www.protondb.com/app/{app_id}"
MAX_APP_ID = 0xFFFFFFFF
METADATA_SEARCH_DEPTH = 5
METADATA_SEARCH_DIRECTORY_LIMIT = 2_000
CACHE_VERSION = 1
CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
CACHE_RETENTION_SECONDS = 90 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class CachedRating:
    rating: str | None
    fetched_at: float


class ProtonDBCache:
    def __init__(self, path: Path | None = None):
        cache_home = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        self.path = path or cache_home / "proton-launcher" / "protondb.json"
        self.entries: dict[int, CachedRating] = {}
        self._load()

    def _load(self) -> None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(value, dict) or value.get("version") != CACHE_VERSION:
            return
        entries = value.get("entries")
        if not isinstance(entries, dict):
            return
        now = time.time()
        for raw_app_id, raw_entry in entries.items():
            if not isinstance(raw_entry, dict):
                continue
            try:
                app_id = int(raw_app_id)
            except (TypeError, ValueError):
                continue
            rating = raw_entry.get("rating")
            fetched_at = raw_entry.get("fetched_at")
            if (
                not 0 < app_id <= MAX_APP_ID
                or not (rating is None or isinstance(rating, str))
                or not isinstance(fetched_at, int | float)
                or isinstance(fetched_at, bool)
                or fetched_at <= 0
                or now - fetched_at > CACHE_RETENTION_SECONDS
            ):
                continue
            self.entries[app_id] = CachedRating(rating, float(fetched_at))

    def lookup(
        self, app_id: int, now: float | None = None
    ) -> tuple[bool, str | None, bool]:
        entry = self.entries.get(app_id)
        if not entry:
            return False, None, False
        current_time = time.time() if now is None else now
        fresh = current_time - entry.fetched_at <= CACHE_MAX_AGE_SECONDS
        return True, entry.rating, fresh

    def put(
        self, app_id: int, rating: str | None, fetched_at: float | None = None
    ) -> None:
        self.entries[app_id] = CachedRating(
            rating, time.time() if fetched_at is None else fetched_at
        )
        try:
            self._save()
        except OSError:
            pass

    def __setitem__(self, app_id: int, rating: str | None) -> None:
        self.put(app_id, rating)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "version": CACHE_VERSION,
            "entries": {
                str(app_id): {
                    "rating": entry.rating,
                    "fetched_at": entry.fetched_at,
                }
                for app_id, entry in sorted(self.entries.items())
            },
        }
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}-", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


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
    if not value.isascii() or not value.isdigit():
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


def _steam_app_id(path: Path) -> int | None:
    try:
        return _app_id(path.read_text(encoding="utf-8-sig", errors="replace"))
    except OSError:
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
            app_id = _steam_app_id(steam_appid)
            if app_id:
                return app_id
    if executable_directory:
        for steam_appid in _nested_metadata_files(
            executable_directory, "steam_appid.txt"
        ):
            app_id = _steam_app_id(steam_appid)
            if app_id:
                return app_id
    return None
