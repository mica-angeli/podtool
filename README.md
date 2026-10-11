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
