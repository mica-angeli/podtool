# podtool

A command line tool for working with an iPod's music database, built on
[pyPodLib](https://github.com/La22e/pyPodLib).

This first version is read-only: it parses an `iTunesDB` file and prints its
metadata.

## Usage

podtool is managed with [uv](https://docs.astral.sh/uv/), which creates the
virtual environment (`.venv`) and installs dependencies on first run:

```console
uv run podtool info ~/Downloads/iTunesDB        # database summary
uv run podtool tracks ~/Downloads/iTunesDB      # one line per track
uv run podtool playlists ~/Downloads/iTunesDB   # playlists and their sizes
```

The database argument is either an `iTunesDB` file or the mount point of an
iPod (e.g. `/Volumes/IPOD`), in which case `iPod_Control/iTunes/iTunesDB` is
used.

Every command accepts `--json`. `tracks` also takes:

| Option | Effect |
|---|---|
| `-n N`, `--limit N` | show at most N tracks |
| `-s TEXT`, `--search TEXT` | only tracks whose title, artist, album or genre contains TEXT |
| `-l`, `--long` | print every metadata field, one block per track |
| `--all-fields` | with `--json`, dump every raw field pyPodLib parsed |

`podtool -v ...` shows pyPodLib's log output.

## Simplifying artists

```console
uv run podtool simplify-artists /Volumes/IPOD --dry-run   # list the edits only
uv run podtool simplify-artists /Volumes/IPOD             # make them and save
```

For each track, the artist and the album artist are split on commas. If the
first name is, on its own, the whole artist of some track in the library, the
credit is reduced to that name: `bbno$, Ironmouse` becomes `bbno$` when the
library has a track by just `bbno$`. Otherwise the credit is left alone, so
`Earth, Wind & Fire` survives unless there is also a track by `Earth`. Only
commas separate names, and matching is exact.

Each edit is printed as it is made. Saving rewrites the whole database through
pyPodLib, which signs it for the device, so it needs a mounted iPod; the
previous database is kept beside it as `iTunesDB.backup`. `--dry-run` also
works on a standalone `iTunesDB` file.

## Compressing lossless tracks

```console
uv run podtool compress /Volumes/IPOD --dry-run   # list the lossless tracks only
uv run podtool compress /Volumes/IPOD             # re-encode them in place
```

Every track whose file holds Apple Lossless audio is re-encoded as 320 kbps
AAC, and the new file takes the old one's place under the same name. The
lossless originals are not kept. Tracks are found by reading the files, because
the database of an iPod filled through libgpod calls every `.m4a` an "AAC audio
file". This needs `ffmpeg` on the `PATH`; Apple's encoder is used where ffmpeg
has it, as it does on macOS.

The tags are not parsed and rewritten but copied byte for byte from the old
file, so all of them survive, artwork and atoms that no tagger knows included.
The one exception is iTunes' gapless tag, `iTunSMPB`, whose numbers are
corrected when it is present because they describe the encoding.

In the database, only the numbers that describe the file change: each
compressed track's size, bit rate, sample rate (audio above 48 kHz is resampled
to 48 kHz) and the encoder delay, padding and sample count that gapless
playback uses. Titles, artists, ratings, play counts and playlists stay as
they are. The database is saved the same way as by `simplify-artists`.

Each new file is checked before it replaces the original: it must carry the
same tags, be the same length and decode without error. A track that fails is
reported and left alone. WAV and AIFF tracks are reported and left alone too.

The run can be interrupted with Ctrl-C and started again later; the tracks
already compressed are saved to the database first. If it is cut off harder
than that, they are in a journal on the iPod, which the next run saves.
`-j N` sets how many tracks are encoded at a time (4 by default).
