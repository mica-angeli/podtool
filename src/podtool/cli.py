"""podtool command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from pypodlib import IPod, Playlist, Track

from . import compress
from .artists import plan_edits
from .compress import CompressError
from .database import Database, DatabaseError, ipod_root, load, open_ipod

_CHECKSUM_NAMES = {0: "none", 1: "HASH58", 2: "HASH72", 3: "HASHAB"}


# ── Formatting helpers ──────────────────────────────────────────────────────


def _duration(ms: int) -> str:
    seconds = round(ms / 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes}:{seconds:02}"


def _long_duration(ms: int) -> str:
    minutes = round(ms / 60000)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = [(days, "d"), (hours, "h"), (minutes, "m")]
    return " ".join(f"{n}{unit}" for n, unit in parts if n) or "0m"


def _size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            break
        value /= 1024
    else:
        unit = "TB"
    return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"


def _timestamp(value: Any) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")


def _utc_offset(seconds: int) -> str:
    sign = "-" if seconds < 0 else "+"
    hours, minutes = divmod(abs(seconds) // 60, 60)
    return f"UTC{sign}{hours:02}:{minutes:02}"


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], right: Sequence[int] = ()) -> None:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]

    def line(cells: Sequence[str]) -> str:
        parts = (c.rjust(w) if i in right else c.ljust(w) for i, (c, w) in enumerate(zip(cells, widths)))
        return "  ".join(parts).rstrip()

    print(line(headers))
    print(line(["-" * w for w in widths]))
    for row in rows:
        print(line(row))


def _fields(pairs: Sequence[tuple[str, Any]]) -> None:
    width = max(len(label) for label, _ in pairs)
    for label, value in pairs:
        print(f"{label + ':':<{width + 1}} {value}")


def _jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _print_json(value: Any) -> None:
    json.dump(_jsonable(value), sys.stdout, indent=2, ensure_ascii=False)
    print()


# ── Commands ────────────────────────────────────────────────────────────────


def _playlist_kind(playlist: Playlist) -> str:
    if playlist.master:
        return "master"
    if playlist.get("is_folder"):
        return "folder"
    if playlist.get("mhsd5_type"):
        # Built-in browse categories (Music, Movies, ...), not user playlists.
        return "category"
    if playlist.is_smart:
        return "smart"
    if playlist.is_podcast:
        return "podcast"
    return "regular"


def _user_playlists(db: Database) -> list[Playlist]:
    """Standard and smart playlists; the podcast dataset mirrors the
    standard one, so only its playlists not already listed are added."""
    lib = db.library
    playlists = [*lib.playlists, *lib.smart_playlists]
    seen = {p.playlist_id for p in playlists}
    playlists += [p for p in lib.podcast_playlists if p.playlist_id not in seen]
    return playlists


def cmd_info(db: Database, args: argparse.Namespace) -> None:
    lib = db.library
    header = db.header
    tracks = lib.tracks
    master = lib.master_playlist
    playlists = [p for p in _user_playlists(db) if _playlist_kind(p) not in ("master", "category")]
    filetypes = Counter(t.get("Filetype") or t.filetype or "unknown" for t in tracks)
    summary = {
        "path": str(db.path),
        "file_size": db.path.stat().st_size,
        "ipod_name": master.name if master else None,
        "db_version": header.get("version"),
        "db_id": header.get("db_id"),
        "language": (header.get("language") or b"").decode("ascii", "replace").strip("\x00"),
        "timezone_offset": header.get("timezone_offset"),
        "checksum": _CHECKSUM_NAMES.get(header.get("hashing_scheme"), str(header.get("hashing_scheme"))),
        "tracks": len(tracks),
        "albums": len(db.albums),
        "artists": len(db.artists),
        "playlists": len(playlists),
        "total_duration_ms": sum(t.length for t in tracks),
        "total_media_bytes": sum(t.size for t in tracks),
        "filetypes": dict(filetypes.most_common()),
    }
    if args.json:
        _print_json(summary)
        return

    _fields(
        [
            ("File", f"{summary['path']} ({_size(summary['file_size'])})"),
            ("iPod name", summary["ipod_name"] or "-"),
            ("DB version", f"{summary['db_version']} (0x{summary['db_version']:02X})"),
            ("DB id", f"{summary['db_id']:016X}"),
            ("Language", summary["language"] or "-"),
            ("Timezone", _utc_offset(summary["timezone_offset"] or 0)),
            ("Checksum", summary["checksum"]),
            ("Tracks", summary["tracks"]),
            ("Albums", summary["albums"]),
            ("Artists", summary["artists"]),
            ("Playlists", summary["playlists"]),
            ("Play time", _long_duration(summary["total_duration_ms"])),
            ("Media size", _size(summary["total_media_bytes"])),
        ]
    )
    if filetypes:
        print("\nFile types:")
        for name, count in filetypes.most_common():
            print(f"  {count:>6}  {name}")


def _track_summary(track: Track) -> dict[str, Any]:
    return {
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "album_artist": track.album_artist,
        "genre": track.genre,
        "composer": track.composer,
        "year": track.year,
        "track_number": track.track_number,
        "total_tracks": track.total_tracks,
        "disc_number": track.disc_number,
        "total_discs": track.total_discs,
        "length_ms": track.length,
        "size": track.size,
        "bitrate": track.bitrate,
        "sample_rate": track.sample_rate,
        "filetype": track.get("Filetype") or track.filetype,
        "rating": track.rating,
        "play_count": track.play_count,
        "skip_count": track.skip_count,
        "date_added": track.get("date_added") or 0,
        "last_played": track.get("last_played") or 0,
        "location": track.location,
        "db_track_id": track.db_track_id,
    }


def _matches(track: Track, needle: str) -> bool:
    haystack = (track.title, track.artist, track.album, track.album_artist, track.genre)
    return any(needle in field.casefold() for field in haystack)


def cmd_tracks(db: Database, args: argparse.Namespace) -> None:
    tracks = db.library.tracks
    if args.search:
        needle = args.search.casefold()
        tracks = [t for t in tracks if _matches(t, needle)]
    total = len(tracks)
    if args.limit is not None:
        tracks = tracks[: args.limit]

    if args.json:
        _print_json([t.data if args.all_fields else _track_summary(t) for t in tracks])
        return

    if args.long:
        for index, track in enumerate(tracks):
            if index:
                print()
            info = _track_summary(track)
            _fields(
                [
                    ("Title", info["title"] or "-"),
                    ("Artist", info["artist"] or "-"),
                    ("Album", info["album"] or "-"),
                    ("Album artist", info["album_artist"] or "-"),
                    ("Genre", info["genre"] or "-"),
                    ("Composer", info["composer"] or "-"),
                    ("Year", info["year"] or "-"),
                    ("Track", f"{info['track_number']}/{info['total_tracks']}"),
                    ("Disc", f"{info['disc_number']}/{info['total_discs']}"),
                    ("Length", _duration(info["length_ms"])),
                    ("Size", _size(info["size"])),
                    ("Format", f"{info['filetype']}, {info['bitrate']} kbps, {info['sample_rate']} Hz"),
                    ("Rating", f"{info['rating'] // 20}/5"),
                    ("Plays", info["play_count"]),
                    ("Skips", info["skip_count"]),
                    ("Added", _timestamp(info["date_added"])),
                    ("Last played", _timestamp(info["last_played"])),
                    ("Location", info["location"] or "-"),
                    ("Track id", f"{info['db_track_id']:016X}"),
                ]
            )
    else:
        rows = [
            [_clip(t.title, 40), _clip(t.artist, 28), _clip(t.album, 32), str(t.year or ""), _duration(t.length)]
            for t in tracks
        ]
        _table(["Title", "Artist", "Album", "Year", "Time"], rows, right=(4,))

    if len(tracks) < total:
        print(f"\nShowing {len(tracks)} of {total} tracks.", file=sys.stderr)


def cmd_playlists(db: Database, args: argparse.Namespace) -> None:
    lengths = {t.db_track_id: t.length for t in db.library.tracks}
    rows = []
    for playlist in _user_playlists(db):
        kind = _playlist_kind(playlist)
        track_ids = playlist.track_ids
        # Smart playlists are evaluated on the iPod, so the file stores no members.
        stored = bool(track_ids) or kind not in ("smart", "category")
        rows.append(
            {
                "name": playlist.name,
                "kind": kind,
                "tracks": len(track_ids) if stored else None,
                "total_duration_ms": sum(lengths.get(i, 0) for i in track_ids) if stored else None,
                "playlist_id": playlist.playlist_id,
            }
        )
    if args.json:
        _print_json(rows)
        return
    _table(
        ["Name", "Kind", "Tracks", "Time"],
        [
            [
                _clip(r["name"], 48),
                r["kind"],
                "-" if r["tracks"] is None else str(r["tracks"]),
                "-" if r["total_duration_ms"] is None else _long_duration(r["total_duration_ms"]),
            ]
            for r in rows
        ],
        right=(2, 3),
    )


def _editable_tracks(ipod: IPod) -> list[Track]:
    # Imported late: pyPodLib's sync package logs warnings on import.
    from pypodlib.sync._db_io import commit_playcounts_if_needed

    try:
        # Saving folds pending play counts into the database, so let
        # pyPodLib commit and clear them first rather than leave the
        # Play Counts file behind to be counted twice.
        commit_playcounts_if_needed(Path(ipod.path))
        return ipod.library(reload=True).tracks
    except Exception as exc:
        raise DatabaseError(f"could not read the iPod's database: {exc}") from exc


def _save(ipod: IPod) -> None:
    try:
        ipod.save()
    except Exception as exc:
        raise DatabaseError(f"saving the database failed: {exc}") from exc


def cmd_simplify_artists(args: argparse.Namespace) -> None:
    ipod = None
    if args.dry_run:
        tracks = load(args.database).library.tracks
    else:
        ipod = open_ipod(args.database)
        tracks = _editable_tracks(ipod)

    edits = plan_edits(tracks)
    for edit in edits:
        edit.apply()
        if not args.json:
            print(f"{edit.track.title or '(untitled)'}: {edit.label} {edit.old!r} -> {edit.new!r}", flush=True)

    changed_tracks = len({id(e.track.data) for e in edits})
    if args.json:
        _print_json(
            [
                {"db_track_id": e.track.db_track_id, "title": e.track.title, "field": e.label, "old": e.old, "new": e.new}
                for e in edits
            ]
        )
    summary = f"{len(edits)} edits to {changed_tracks} of {len(tracks)} tracks"
    if ipod is None:
        print(f"Dry run: {summary}; nothing was written.", file=sys.stderr)
    elif not edits:
        print("Nothing to simplify; the database was left untouched.", file=sys.stderr)
    else:
        _save(ipod)
        print(f"Saved {summary} to {ipod.path}.", file=sys.stderr)


def _check_saveable(ipod: IPod, tracks: Sequence[Track]) -> None:
    """Run, on the database as it stands, the checks that pyPodLib fails a
    save for, such as a track whose file is missing."""
    # Imported late: pyPodLib's sync package logs warnings on import.
    from pypodlib.sync._db_io import verify_written_database

    try:
        verify_written_database(Path(ipod.path), expected_track_count=len(tracks))
    except Exception as exc:
        raise DatabaseError(f"pyPodLib would not save this iPod's database, so nothing was compressed: {exc}") from exc


def _track_label(track: Track) -> str:
    return f"{track.artist or '(no artist)'} - {track.title or '(untitled)'}"


def _find_lossless(root: Path, tracks: Sequence[Track]) -> list[tuple[Track, Path, int]]:
    """Return each lossless track with its file and the file's size.

    Every file is opened to check, which takes a while on an iPod's disk.
    """
    tty = sys.stderr.isatty()
    newline = "\n" if tty else ""
    found = []
    for index, track in enumerate(tracks, 1):
        if tty:
            print(f"\rChecking files: {index}/{len(tracks)}", end="", file=sys.stderr, flush=True)
        try:
            path = compress.lossless_file(root, track)
            if path is not None:
                found.append((track, path, path.stat().st_size))
        except (CompressError, OSError) as exc:
            print(f"{newline}podtool: skipping {_track_label(track)}: {exc}", file=sys.stderr)
    if tty:
        print(file=sys.stderr)
    return found


def cmd_compress(args: argparse.Namespace) -> None:
    root = ipod_root(args.database)
    if root is None:
        raise DatabaseError(f"{args.database} is not on an iPod; compress works on the music files beside the database")
    tracks = load(root).library.tracks
    lossless = _find_lossless(root, tracks)

    if args.dry_run:
        if args.json:
            _print_json([{**_track_summary(t), "file": str(path), "file_size": size} for t, path, size in lossless])
        elif lossless:
            rows = [
                [_clip(t.title, 40), _clip(t.artist, 28), _clip(t.album, 32), _duration(t.length), _size(size)]
                for t, _, size in lossless
            ]
            _table(["Title", "Artist", "Album", "Time", "Size"], rows, right=(3, 4))
        total = sum(size for _, _, size in lossless)
        estimate = sum(t.length for t, _, _ in lossless) * compress.BITRATE_KBPS // 8
        print(
            f"Dry run: {len(lossless)} of {len(tracks)} tracks are lossless, {_size(total)} that would "
            f"compress to about {_size(estimate)}; nothing was written.",
            file=sys.stderr,
        )
        return

    encoder = compress.find_encoder()
    # Before any file is replaced, so that an iPod whose database could not
    # be updated afterwards is left alone.
    ipod = open_ipod(args.database)
    _check_saveable(ipod, tracks)

    results = []
    failed = 0
    pool = ThreadPoolExecutor(max(1, args.jobs))
    encodes = {pool.submit(compress.encode, path, encoder): (track, size) for track, path, size in lossless}
    try:
        for encode in as_completed(encodes):
            track, size = encodes.pop(encode)
            label = f"[{len(results) + failed + 1}/{len(lossless)}] {_track_label(track)}"
            try:
                encoded = encode.result()
            except CompressError as exc:
                failed += 1
                print(f"podtool: {label}: not compressed: {exc}", file=sys.stderr)
                continue
            compress.replace(root, track, encoded)
            new_size = encoded.fields["size"]
            results.append({"db_track_id": track.db_track_id, "title": track.title, "artist": track.artist, "old_size": size, "new_size": new_size})
            if not args.json:
                print(f"{label}: {_size(size)} -> {_size(new_size)}", flush=True)
    finally:
        # Also reached when interrupted: stop encoding, then record what was done.
        pool.shutdown(cancel_futures=True)
        for encode in encodes:
            if not encode.cancelled() and encode.exception() is None:
                compress.discard(encode.result().path)
        updated = 0
        if compress.has_journal(root):
            # Reread the database rather than save the copy read before hours
            # of encoding.  The journal may also hold what an earlier run was
            # stopped before it could save.
            updated = compress.apply_journal(root, _editable_tracks(ipod))
            if updated:
                _save(ipod)
            compress.clear_journal(root)
        if args.json:
            _print_json(results)
        freed = sum(r["old_size"] - r["new_size"] for r in results)
        print(
            f"Compressed {len(results)} of {len(lossless)} lossless tracks, freeing {_size(freed)}; "
            + (f"updated {updated} tracks in the database on {ipod.path}." if updated else "the database was left untouched."),
            file=sys.stderr,
        )
    if failed:
        raise CompressError(f"{failed} of the lossless tracks could not be compressed")


# ── Entry point ─────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="podtool", description="Inspect an iPod's iTunesDB music database.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('podtool')}")
    parser.add_argument("-v", "--verbose", action="store_true", help="show pyPodLib's log output")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("database", help="path to an iTunesDB file, or to a mounted iPod")
    common.add_argument("--json", action="store_true", help="print JSON instead of text")

    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    info = commands.add_parser("info", parents=[common], help="summarize the database")
    info.set_defaults(func=cmd_info)

    tracks = commands.add_parser("tracks", parents=[common], help="list track metadata")
    tracks.add_argument("-n", "--limit", type=int, metavar="N", help="show at most N tracks")
    tracks.add_argument("-s", "--search", metavar="TEXT", help="only tracks whose title, artist, album or genre contains TEXT")
    tracks.add_argument("-l", "--long", action="store_true", help="print every metadata field, one block per track")
    tracks.add_argument("--all-fields", action="store_true", help="with --json, dump every raw field pyPodLib parsed")
    tracks.set_defaults(func=cmd_tracks)

    playlists = commands.add_parser("playlists", parents=[common], help="list playlists")
    playlists.set_defaults(func=cmd_playlists)

    simplify = commands.add_parser(
        "simplify-artists",
        parents=[common],
        help="reduce 'A, B' artist credits to 'A' when A has tracks of its own",
        description="Rewrite comma-separated artist and album artist credits to just their first "
        "name, but only when another track in the library is credited to that name alone. "
        "Saving requires a mounted iPod; --dry-run also works on a standalone iTunesDB file.",
    )
    simplify.add_argument("-n", "--dry-run", action="store_true", help="list the edits without saving them")
    simplify.set_defaults(func=cmd_simplify_artists)

    compress_parser = commands.add_parser(
        "compress",
        parents=[common],
        help=f"re-encode lossless tracks as {compress.BITRATE_KBPS} kbps AAC",
        description=f"Replace every Apple Lossless file on a mounted iPod with a {compress.BITRATE_KBPS} kbps AAC "
        "encoding of it that carries the same tags.  In the database only each track's size, bit "
        "rate and gapless playback numbers change.  Needs ffmpeg.  The lossless originals are "
        "not kept, and a run that is interrupted can be resumed by running it again.",
    )
    compress_parser.add_argument("-n", "--dry-run", action="store_true", help="list the lossless tracks without compressing them")
    compress_parser.add_argument("-j", "--jobs", type=int, default=min(4, os.cpu_count() or 1), metavar="N", help="encode N tracks at a time (default: %(default)s)")
    compress_parser.set_defaults(func=cmd_compress)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.CRITICAL, format="%(name)s: %(message)s")
    try:
        if args.func in (cmd_simplify_artists, cmd_compress):
            args.func(args)
        else:
            args.func(load(args.database), args)
        sys.stdout.flush()
    except (DatabaseError, CompressError) as exc:
        print(f"podtool: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # Output was piped into something like `head` that stopped reading.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0
