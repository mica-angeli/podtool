"""Re-encoding an iPod's Apple Lossless tracks as 320 kbps AAC."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mutagen import MutagenError
from mutagen.mp4 import MP4, AtomError, Atoms, MP4Info
from pypodlib import Track

BITRATE_KBPS = 320

# The highest sample rate an iPod plays.
_MAX_SAMPLE_RATE = 48000
# Samples per AAC frame.
_FRAME = 1024
# The children of an MP4's moov box that hold tags rather than audio structure.
_TAG_BOXES = (b"udta", b"meta")
# Lossless formats that keep their tags as ID3, which cannot be carried into
# an MP4 unchanged.
_PCM_SUFFIXES = (".wav", ".aif", ".aiff")
# iTunes' gapless tag: reserved, encoder delay, padding and sample count, in hex.
_ITUNSMPB = re.compile(rb"(iTunSMPB.{4}data.{8}) [0-9A-F]{8} [0-9A-F]{8} [0-9A-F]{8} [0-9A-F]{16}", re.DOTALL | re.IGNORECASE)
# Database updates for files already replaced, kept on the iPod until saved.
_JOURNAL = Path("iPod_Control") / "iTunes" / "podtool-compress.journal"


class CompressError(Exception):
    """A track, or the whole library, could not be compressed."""


@dataclass(frozen=True)
class Encoded:
    """An AAC replacement for a track's file, waiting beside it."""

    path: Path
    # The database fields that describe the file, as pyPodLib names them.
    fields: dict[str, int]


def track_file(root: Path, track: Track) -> Path:
    return root.joinpath(*track.location.strip(":").split(":"))


def lossless_file(root: Path, track: Track) -> Path | None:
    """Return the track's file if it holds Apple Lossless audio.

    The file decides, not the database: libgpod labels every .m4a an "AAC
    audio file".  Raises CompressError for a file that is, or may be,
    lossless but cannot be compressed.
    """
    path = track_file(root, track)
    suffix = path.suffix.lower()
    if suffix in _PCM_SUFFIXES:
        raise CompressError("uncompressed audio is not supported, only Apple Lossless")
    if suffix != ".m4a":
        return None
    try:
        with open(path, "rb") as f:
            codec = MP4Info(Atoms(f), f).codec
    except OSError as exc:
        raise CompressError(f"could not read {path}: {exc.strerror}") from exc
    except (AtomError, MutagenError) as exc:
        raise CompressError(f"could not read {path}: {exc}") from exc
    return path if codec == "alac" else None


def find_encoder() -> list[str]:
    """Return ffmpeg's options for the best AAC encoder it has here."""
    if shutil.which("ffmpeg") is None:
        raise CompressError("ffmpeg is needed to compress tracks, but was not found")
    listing = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if re.search(r"^\s*A\S*\s+aac_at\s", listing, re.MULTILINE):
        # Apple's encoder, the one iTunes uses.
        return ["-c:a", "aac_at", "-aac_at_mode", "cbr"]
    return ["-c:a", "aac"]


def _box(f, atom) -> bytes:
    f.seek(atom.offset)
    return f.read(atom.length)


def _payload(f, atom) -> bytes:
    ok, data = atom.read(f)
    if not ok:
        raise CompressError(f"truncated {atom.name.decode('latin-1')} box")
    return data


def _tags(path: Path) -> dict:
    tags = MP4(path).tags
    return {key: value for key, value in (tags or {}).items() if not key.endswith(":iTunSMPB")}


def _ffmpeg(*args: str) -> None:
    # Stop at damaged audio rather than work around it.
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-xerror", *args]
    result = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if result.returncode:
        message = (result.stderr.strip().splitlines() or [f"exit status {result.returncode}"])[0]
        raise CompressError("ffmpeg: " + re.sub(r"^(\[[^]]*\]\s*)+", "", message))


def encode(source: Path, encoder: Sequence[str]) -> Encoded:
    """Encode *source* to AAC in a file beside it, carrying over its tags.

    The tags are copied byte for byte rather than parsed and rewritten, so
    everything in them survives, artwork and unknown atoms included.  Only
    an iTunSMPB tag is changed, since it describes the encoding.
    """
    target = source.with_name(source.name + ".part")
    finished = False
    try:
        with tempfile.TemporaryDirectory(prefix="podtool-") as scratch:
            fields = _encode(source, Path(scratch) / "audio.m4a", target, encoder)
        finished = True
        return Encoded(target, fields)
    except (OSError, AtomError, MutagenError, KeyError, struct.error) as exc:
        raise CompressError(str(exc)) from exc
    finally:
        if not finished:
            discard(target)


def _encode(source: Path, audio: Path, target: Path, encoder: Sequence[str]) -> dict[str, int]:
    with open(source, "rb") as f:
        atoms = Atoms(f)
        info = MP4Info(atoms, f)
        tags = b"".join(_box(f, atom) for atom in atoms[b"moov"].children if atom.name in _TAG_BOXES)
    rate = min(info.sample_rate, _MAX_SAMPLE_RATE)
    samples = round(info.length * rate)

    # The audio alone, on this computer.  ffmpeg ends the file with moov.
    _ffmpeg("-i", str(source), "-map", "0:a:0", *encoder, "-b:a", f"{BITRATE_KBPS}k", "-ar", str(rate), "-map_metadata", "-1", "-f", "ipod", str(audio))

    with open(audio, "rb") as f:
        atoms = Atoms(f)
        ftyp, moov = atoms.atoms[0], atoms.atoms[-1]
        encoded = MP4Info(atoms, f)
        if (ftyp.name, moov.name) != (b"ftyp", b"moov") or not encoded.codec.startswith("mp4a") or encoded.sample_rate != rate:
            raise CompressError("ffmpeg wrote an unexpected file")
        # The encoder's delay is where the track's one edit starts, and its
        # padding is whatever the whole frames hold past the audio.
        (pregap,) = struct.unpack_from(">i", _payload(f, moov[b"trak", b"edts"]), 20)
        (frames,) = struct.unpack_from(">I", _payload(f, moov[b"trak", b"mdia", b"minf", b"stbl", b"stsz"]), 8)
        postgap = frames * _FRAME - pregap - samples
        if pregap < 0 or not 0 <= postgap < 4 * _FRAME:
            raise CompressError("the encoded audio is not the length of the original")

        # Rebuild moov with the source's tags, and move it ahead of the
        # audio, where iTunes puts it.  That pushes the audio back by
        # moov's size, so the tables that locate it are adjusted to match.
        body = bytearray()
        tables = []
        for atom in moov.children:
            if atom.name in _TAG_BOXES:
                continue
            if atom.name == b"trak":
                for table in atom[b"mdia", b"minf", b"stbl"].children:
                    if table.name in (b"stco", b"co64"):
                        tables.append((len(body) + table.offset - atom.offset, "I" if table.name == b"stco" else "Q"))
            body += _box(f, atom)
        gapless = b" 00000000 %08X %08X %016X" % (pregap, postgap, samples)
        body += _ITUNSMPB.sub(lambda match: match[1] + gapless, tags)
        moved = 8 + len(body)
        for at, width in tables:
            (count,) = struct.unpack_from(">I", body, at + 12)
            offsets = struct.unpack_from(f">{count}{width}", body, at + 16)
            struct.pack_into(f">{count}{width}", body, at + 16, *(offset + moved for offset in offsets))

        with open(target, "wb") as out:
            out.write(_box(f, ftyp))
            out.write(struct.pack(">I4s", moved, b"moov") + body)
            f.seek(ftyp.length)
            remaining = moov.offset - ftyp.length
            while remaining:
                chunk = f.read(min(remaining, 1 << 20))
                if not chunk:
                    raise CompressError("ffmpeg wrote a truncated file")
                out.write(chunk)
                remaining -= len(chunk)
            out.flush()
            os.fsync(out.fileno())

    # The original is about to be lost, so check what replaces it.
    if _tags(target) != _tags(source):
        raise CompressError("the tags did not survive encoding")
    _ffmpeg("-i", str(target), "-map", "0:a:0", "-f", "null", "-")
    return {
        "size": target.stat().st_size,
        "bitrate": encoded.bitrate // 1000,
        "sample_rate_1": rate,
        "pregap": pregap,
        "postgap": postgap,
        "sample_count": samples,
    }


def discard(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def replace(root: Path, track: Track, encoded: Encoded) -> None:
    """Put an encoded file in the place of the track's own.

    The database fields that describe the new file are journaled on the iPod
    first, so that a run cut short before the database is saved leaves a
    record for the next one to save.
    """
    target = track_file(root, track)
    try:
        with open(root / _JOURNAL, "a") as f:
            # The newline leads, to end any line a crash left half written.
            f.write("\n" + json.dumps({"db_track_id": track.db_track_id, **encoded.fields}))
            f.flush()
            os.fsync(f.fileno())
        os.replace(encoded.path, target)
    except OSError as exc:
        discard(encoded.path)
        raise CompressError(f"could not replace {target}: {exc}") from exc


def has_journal(root: Path) -> bool:
    return (root / _JOURNAL).exists()


def apply_journal(root: Path, tracks: Sequence[Track]) -> int:
    """Update *tracks* to describe the files that replace() put in place,
    and return how many were updated.

    Nothing else about a track is touched.  An entry is skipped unless its
    file is there, which is how one journaled just before a replacement
    that failed is told apart.
    """
    by_id = {track.db_track_id: track for track in tracks}
    updated = 0
    for line in (root / _JOURNAL).read_text().splitlines():
        try:
            fields = json.loads(line)
            track = by_id[fields.pop("db_track_id")]
            if track_file(root, track).stat().st_size != fields["size"]:
                continue
        except (ValueError, KeyError, OSError):
            # A line cut short, or a track or file that has since gone.
            continue
        for name, value in fields.items():
            track[name] = value
        updated += 1
    return updated


def clear_journal(root: Path) -> None:
    (root / _JOURNAL).unlink(missing_ok=True)
