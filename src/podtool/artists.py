"""Simplifying multi-artist credits down to their lead artist."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pypodlib import Track

# (label, parsed field, matching sort field)
_FIELDS = (
    ("artist", "Artist", "Sort Artist"),
    ("album artist", "Album Artist", "Sort Album Artist"),
)


@dataclass(frozen=True)
class Edit:
    """One credit to rewrite on one track."""

    track: Track
    label: str
    field: str
    old: str
    new: str
    sort_field: str
    new_sort: str

    def apply(self) -> None:
        self.track[self.field] = self.new
        self.track[self.sort_field] = self.new_sort


def plan_edits(tracks: Sequence[Track]) -> list[Edit]:
    """Find comma-separated artist and album artist credits whose first
    name is, on its own, the whole artist of some track.

    "Earth, Wind & Fire" is left alone unless the library also has a track
    by just "Earth"; "bbno$, Ironmouse" becomes "bbno$" when it has one by
    just "bbno$".  Matching is exact.  Edits only ever produce names that
    already stood alone, so a second pass finds nothing new.
    """
    solo = {t.artist.strip() for t in tracks}
    # Reuse the sort name the solo tracks already carry, so an edited track
    # sorts together with them.
    solo_sort: dict[str, str] = {}
    for track in tracks:
        sort_name = track.get("Sort Artist")
        if sort_name:
            solo_sort.setdefault(track.artist.strip(), sort_name)

    edits = []
    for track in tracks:
        for label, field, sort_field in _FIELDS:
            value = track.get(field) or ""
            if "," not in value:
                continue
            first = value.split(",")[0].strip()
            if not first or first not in solo:
                continue
            edits.append(Edit(track, label, field, value, first, sort_field, solo_sort.get(first, "")))
    return edits
