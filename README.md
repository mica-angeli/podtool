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
