"""Loading an iTunesDB through pyPodLib."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pypodlib
from pypodlib import IPod, Library
from pypodlib.itunesdb_parser import parse_itunesdb
from pypodlib.itunesdb_parser.ipod_library import load_ipod_library

# Where the database lives relative to an iPod's mount point.
_ITUNES_DIR = Path("iPod_Control") / "iTunes"
_DB_NAMES = ("iTunesDB", "iTunesCDB")
_MAGIC = b"mhbd"


class DatabaseError(Exception):
    """The database could not be found or parsed."""


@dataclass
class Database:
    """A parsed iTunesDB: the file header plus pyPodLib's library view."""

    path: Path
    header: dict[str, Any]
    albums: list[dict[str, Any]]
    artists: list[dict[str, Any]]
    library: Library


def resolve_path(path: str | Path) -> Path:
    """Return the database file for *path*, which may be the file itself
    or the root of a mounted iPod."""
    path = Path(path).expanduser()
    if path.is_dir():
        for name in _DB_NAMES:
            candidate = path / _ITUNES_DIR / name
            if candidate.is_file():
                return candidate
        raise DatabaseError(f"no iTunesDB found under {path / _ITUNES_DIR}")
    if not path.is_file():
        raise DatabaseError(f"no such file: {path}")
    return path


def load(path: str | Path) -> Database:
    """Parse the iTunesDB at *path* (a file, or an iPod mount point)."""
    db_path = resolve_path(path)
    with open(db_path, "rb") as fh:
        if fh.read(4) != _MAGIC:
            raise DatabaseError(f"{db_path} is not an iTunesDB (missing 'mhbd' header)")
    data = load_ipod_library(str(db_path))
    if data is None:
        # load_ipod_library swallows the failure; parse again for the reason.
        try:
            parse_itunesdb(db_path)
        except Exception as exc:
            raise DatabaseError(f"{db_path} is not a readable iTunesDB: {exc}") from exc
        raise DatabaseError(f"{db_path} could not be parsed")

    library = Library(
        db_path,
        tracks=data.get("mhlt") or [],
        dataset2_standard_playlists=data.get("mhlp"),
        dataset3_podcast_playlists=data.get("mhlp_podcast"),
        dataset5_smart_playlists=data.get("mhlp_smart"),
    )
    # Everything that isn't a parsed list is a field of the mhbd header.
    header = {k: v for k, v in data.items() if not isinstance(v, list)}
    return Database(
        path=db_path,
        header=header,
        albums=data.get("mhla") or [],
        artists=data.get("mhsd_type_8") or [],
        library=library,
    )


def ipod_root(path: str | Path) -> Path | None:
    """Return the mount point of the iPod that *path* (the mount point, or
    the database file inside it) belongs to, or None for a standalone file."""
    path = Path(path).expanduser()
    if path.is_dir():
        root = path
    elif path.parent.name == _ITUNES_DIR.name and path.parent.parent.name == _ITUNES_DIR.parent.name:
        root = path.parents[2]
    else:
        return None
    resolve_path(root)
    return root


def open_ipod(path: str | Path) -> IPod:
    """Connect to the iPod that *path* (its mount point, or the database
    file inside it) belongs to, for making changes."""
    root = ipod_root(path)
    if root is None:
        # pyPodLib signs the database for one device, so it only writes in place.
        raise DatabaseError(f"{path} is not on an iPod; changes can only be saved to a mounted iPod")
    try:
        return pypodlib.connect(root)
    except ValueError as exc:
        raise DatabaseError(f"could not identify the iPod at {root}: {exc}") from exc
