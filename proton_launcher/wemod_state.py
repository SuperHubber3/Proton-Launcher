# SPDX-License-Identifier: GPL-3.0-only
"""Read WeMod's saved custom-game matches without modifying its database."""

from __future__ import annotations

import json
import os
import re
import struct
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

LEVELDB_BLOCK_SIZE = 32_768
LEVELDB_HEADER_SIZE = 7
GLOBAL_STORE_KEY = b"infinity:globalStore"
MAPPING_FORMAT = "proton-launcher-wemod-mappings"
MAPPING_VERSION = 1


@dataclass(frozen=True, slots=True)
class WeModGameMapping:
    executable: str
    title_id: str
    game_id: str

    @property
    def uri(self) -> str:
        return f"wemod://titles/{self.title_id}?gameId={self.game_id}"


def mapping_file() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / "proton-launcher" / "wemod-games.json"


def normalize_executable_path(value: str) -> str:
    return value.strip().strip('"').replace("/", "\\").casefold()


def _host_executable_key(executable: Path) -> str:
    return str(executable.expanduser().resolve(strict=False)).casefold()


def load_cached_mapping(
    executable: Path, path: Path | None = None
) -> WeModGameMapping | None:
    cache_path = path or mapping_file()
    try:
        value = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("format") != MAPPING_FORMAT
        or value.get("version") != MAPPING_VERSION
        or not isinstance(value.get("games"), dict)
    ):
        return None
    item = value["games"].get(_host_executable_key(executable))
    if not isinstance(item, dict):
        return None
    fields = (item.get("executable"), item.get("title_id"), item.get("game_id"))
    if not all(isinstance(field, str) and field for field in fields):
        return None
    return WeModGameMapping(*fields)


def save_cached_mapping(mapping: WeModGameMapping, path: Path | None = None) -> None:
    cache_path = path or mapping_file()
    try:
        value = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = {}
    except OSError:
        # A transient read failure must not rewrite the cache with only the
        # current game; corrupt content (handled below) is different.
        return
    except (UnicodeError, json.JSONDecodeError):
        value = {}
    if (
        not isinstance(value, dict)
        or value.get("format") != MAPPING_FORMAT
        or value.get("version") != MAPPING_VERSION
        or not isinstance(value.get("games"), dict)
    ):
        value = {
            "format": MAPPING_FORMAT,
            "version": MAPPING_VERSION,
            "games": {},
        }
    value["games"][_host_executable_key(Path(mapping.executable))] = asdict(mapping)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=cache_path.name + ".", dir=cache_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, cache_path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid LevelDB varint")


def _read_blob(data: bytes, offset: int) -> tuple[bytes, int]:
    length, offset = _read_varint(data, offset)
    end = offset + length
    if end > len(data):
        raise ValueError("truncated LevelDB value")
    return data[offset:end], end


def _logical_records(data: bytes) -> Iterator[bytes]:
    fragment: bytearray | None = None
    for block_start in range(0, len(data), LEVELDB_BLOCK_SIZE):
        block = data[block_start : block_start + LEVELDB_BLOCK_SIZE]
        offset = 0
        while offset + LEVELDB_HEADER_SIZE <= len(block):
            _, length, record_type = struct.unpack_from("<IHB", block, offset)
            offset += LEVELDB_HEADER_SIZE
            if length == 0 and record_type == 0:
                break
            end = offset + length
            if end > len(block):
                break
            payload = block[offset:end]
            offset = end
            if record_type == 1:
                fragment = None
                yield payload
            elif record_type == 2:
                fragment = bytearray(payload)
            elif record_type == 3 and fragment is not None:
                fragment.extend(payload)
            elif record_type == 4 and fragment is not None:
                fragment.extend(payload)
                yield bytes(fragment)
                fragment = None


def _write_batch_values(payload: bytes) -> Iterator[tuple[int, bytes, bytes | None]]:
    if len(payload) < 12:
        return
    sequence, count = struct.unpack_from("<QI", payload)
    offset = 12
    try:
        for index in range(count):
            record_type = payload[offset]
            offset += 1
            key, offset = _read_blob(payload, offset)
            value = None
            if record_type == 1:
                value, offset = _read_blob(payload, offset)
            elif record_type != 0:
                raise ValueError("unknown LevelDB write type")
            yield sequence + index, key, value
    except (IndexError, ValueError):
        return


def _decode_local_storage_value(value: bytes) -> str:
    if not value:
        raise ValueError("empty local-storage value")
    if value[0] == 0:
        return value[1:].decode("utf-16le")
    if value[0] == 1:
        return value[1:].decode("latin-1")
    raise ValueError("unknown local-storage string encoding")


def read_global_store(leveldb: Path) -> dict[str, Any] | None:
    # This reads only LevelDB's write-ahead logs. A compacted key that exists
    # only in an .ldb or .sst table is intentionally treated as unavailable.
    latest: tuple[int, bytes | None] | None = None
    for log in leveldb.glob("[0-9]*.log"):
        try:
            data = log.read_bytes()
        except OSError:
            continue
        for record in _logical_records(data):
            for sequence, key, value in _write_batch_values(record):
                if key.endswith(GLOBAL_STORE_KEY) and (
                    latest is None or sequence > latest[0]
                ):
                    latest = sequence, value
    if latest is None or latest[1] is None:
        return None
    try:
        value = json.loads(_decode_local_storage_value(latest[1]))
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _custom_correlations(value: Any) -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                correlation = _parse_custom_correlation(key)
                if correlation:
                    yield correlation
            yield from _custom_correlations(item)
    elif isinstance(value, list):
        for item in value:
            yield from _custom_correlations(item)
    elif isinstance(value, str):
        correlation = _parse_custom_correlation(value)
        if correlation:
            yield correlation


def _parse_custom_correlation(value: str) -> tuple[str, str] | None:
    match = re.search(r"custom:", value, re.IGNORECASE)
    if match is None:
        return None
    game_and_path = value[match.end() :]
    game_id, separator, location = game_and_path.partition("_")
    if not separator or not game_id.isdigit() or not location:
        return None
    path, separator, suffix = location.rpartition(":")
    if separator and suffix.isdigit() and path.casefold().endswith(".exe"):
        location = path
    return game_id, location


def _unique_correlations(*sources: Any) -> Iterator[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    for source in sources:
        for correlation in _custom_correlations(source):
            if correlation not in seen:
                seen.add(correlation)
                yield correlation


def find_custom_mapping(
    state: dict[str, Any], executable: Path, wine_executable: str | list[str]
) -> WeModGameMapping | None:
    wine_executables = (
        [wine_executable] if isinstance(wine_executable, str) else wine_executable
    )
    targets = {normalize_executable_path(value) for value in wine_executables}
    feedback = state.get("trainerFeedbackRequests")
    feedback_keys = list(reversed(feedback)) if isinstance(feedback, dict) else []
    game_id = next(
        (
            candidate_id
            for candidate_id, location in _unique_correlations(
                state.get("installedApps"),
                state.get("installedGameVersions"),
                feedback_keys,
                state,
            )
            if normalize_executable_path(location) in targets
        ),
        None,
    )
    if game_id is None:
        return None
    catalog = state.get("catalog")
    games = catalog.get("games") if isinstance(catalog, dict) else None
    game = games.get(game_id) if isinstance(games, dict) else None
    title_id = game.get("titleId") if isinstance(game, dict) else None
    if not isinstance(title_id, str) or not title_id:
        preferences = state.get("titlePreferences")
        if isinstance(preferences, dict):
            title_id = next(
                (
                    key
                    for key, preference in preferences.items()
                    if isinstance(key, str)
                    and isinstance(preference, dict)
                    and preference.get("selectedGameId") == game_id
                ),
                None,
            )
    if not isinstance(title_id, str) or not title_id:
        return None
    return WeModGameMapping(
        str(executable.expanduser().resolve(strict=False)), title_id, game_id
    )


def discover_custom_mapping(
    wemod_executable: Path,
    executable: Path,
    wine_executable: str | list[str],
) -> WeModGameMapping | None:
    data = wemod_executable.resolve(strict=False).parent.parent / "wemod_login"
    state = read_global_store(data / "Local Storage" / "leveldb")
    return find_custom_mapping(state, executable, wine_executable) if state else None
