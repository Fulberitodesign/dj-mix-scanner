"""Construction de la tracklist chronologique à partir des fenêtres reconnues,
et lecture d'une éventuelle tracklist dans la description / les chapitres YouTube."""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .classify import classify, core_title
from .models import Hit, Match, Track, normalize


@dataclass
class _Run:
    key: str
    match: Match
    first: float
    last: float
    count: int


def build_tracklist(
    hits: list[Hit],
    clip_length: float,
    duration: float | None = None,
    max_gap: float = 120.0,
    min_hits: int = 1,
) -> list[Track]:
    """Regroupe les détections consécutives d'un même morceau.

    - les fenêtres non reconnues (transitions, voix, effets) ne coupent pas un morceau
      tant que le trou fait moins de `max_gap` secondes ;
    - une détection isolée prise en sandwich entre deux détections d'un même morceau
      (A, B, A) est considérée comme un faux positif et absorbée.
    """
    hits = sorted(hits, key=lambda h: h.offset)
    runs: list[_Run] = []
    for h in hits:
        if h.match is None:
            continue
        key = h.match.key
        if runs and runs[-1].key == key and h.offset - runs[-1].last <= max_gap:
            runs[-1].last = h.offset
            runs[-1].count += 1
            _enrich(runs[-1].match, h.match)
        else:
            runs.append(_Run(key, h.match, h.offset, h.offset, 1))

    changed = True
    while changed:
        changed = False
        for i in range(1, len(runs) - 1):
            a, b, c = runs[i - 1], runs[i], runs[i + 1]
            if b.count == 1 and a.key == c.key and c.first - a.last <= max_gap * 2:
                a.last, a.count = c.last, a.count + c.count
                _enrich(a.match, c.match)
                del runs[i : i + 2]
                changed = True
                break

    runs = [r for r in runs if r.count >= min_hits]

    tracks: list[Track] = []
    for i, r in enumerate(runs):
        end = r.last + clip_length
        if i + 1 < len(runs):
            end = min(end, runs[i + 1].first)
        if duration:
            end = min(end, duration)
        m = r.match
        tracks.append(Track(
            index=i + 1, start=r.first, end=max(end, r.first), title=m.title, artist=m.artist,
            version=classify(m.title, m.artist), hits=r.count,
            confidence="high" if r.count >= 3 else "medium" if r.count == 2 else "low",
            isrc=m.isrc, album=m.album, label=m.label, year=m.year, cover=m.cover, links=dict(m.links),
        ))
    return tracks


def _enrich(target: Match, other: Match) -> None:
    """Complète les champs manquants d'une détection avec ceux d'une autre."""
    for f in ("isrc", "album", "label", "year", "cover", "provider_id"):
        if not getattr(target, f) and getattr(other, f):
            setattr(target, f, getattr(other, f))
    for k, v in other.links.items():
        target.links.setdefault(k, v)


# --- Tracklist donnée par l'uploader -------------------------------------------------

_TS = r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})"
_LINE_TS_FIRST = re.compile(rf"^\s*(?:\d+[.)]\s*)?[\[(]?{_TS}[\])]?\s*[-–—|:.]?\s*(.+?)\s*$")
_LINE_TS_LAST = re.compile(rf"^\s*(?:\d+[.)]\s*)?(.+?)\s*[\[(]?{_TS}[\])]?\s*$")


@dataclass
class DescEntry:
    start: float
    artist: str
    title: str


def _secs(h: str | None, m: str, s: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s)


def _split_artist_title(text: str) -> tuple[str, str]:
    text = re.sub(r"^\s*(?:id\s*-\s*id|\?\?\?)\s*$", "ID - ID", text, flags=re.I)
    parts = re.split(r"\s+[-–—]\s+", text, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", text.strip()


def parse_description(description: str, chapters: list[dict] | None = None) -> list[DescEntry]:
    """Extrait une tracklist horodatée des chapitres YouTube ou, à défaut, de la description."""
    entries: list[DescEntry] = []
    if chapters and len(chapters) > 1:
        for ch in chapters:
            artist, title = _split_artist_title(ch.get("title", ""))
            entries.append(DescEntry(float(ch.get("start_time", 0)), artist, title))
        return entries

    for line in (description or "").splitlines():
        m = _LINE_TS_FIRST.match(line)
        if m:
            start, text = _secs(m.group(1), m.group(2), m.group(3)), m.group(4)
        else:
            m = _LINE_TS_LAST.match(line)
            if not m:
                continue
            text, start = m.group(1), _secs(m.group(2), m.group(3), m.group(4))
        text = text.strip(" -–—|")
        if len(text) < 3 or not re.search(r"[A-Za-z]", text):
            continue
        artist, title = _split_artist_title(text)
        entries.append(DescEntry(start, artist, title))
    entries.sort(key=lambda e: e.start)
    return entries if len(entries) >= 2 else []


def similarity(artist_a: str, title_a: str, artist_b: str, title_b: str) -> float:
    t = SequenceMatcher(None, core_title(title_a), core_title(title_b)).ratio()
    if not artist_a or not artist_b:
        return t
    a = SequenceMatcher(None, normalize(artist_a), normalize(artist_b)).ratio()
    return 0.65 * t + 0.35 * a


def merge_with_description(tracks: list[Track], entries: list[DescEntry], window: float = 240.0) -> list[Track]:
    """Croise la détection audio avec la tracklist de l'uploader.

    Les morceaux confirmés des deux côtés passent en source "audio+description" ;
    ceux listés par l'uploader mais non reconnus à l'écoute sont ajoutés (source "description").
    """
    used: set[int] = set()
    for e in entries:
        if normalize(e.title) in ("id", "") or normalize(e.artist) == "id":
            continue
        best, best_score = None, 0.0
        for i, t in enumerate(tracks):
            if i in used or abs(t.start - e.start) > window:
                continue
            score = similarity(t.artist, t.title, e.artist, e.title)
            if score > best_score:
                best, best_score = i, score
        if best is not None and best_score >= 0.6:
            used.add(best)
            tracks[best].source = "audio+description"
            tracks[best].confidence = "high"
            continue
        tracks.append(Track(
            index=0, start=e.start, end=e.start, title=e.title, artist=e.artist,
            version=classify(e.title, e.artist), source="description", confidence="medium",
        ))

    tracks.sort(key=lambda t: t.start)
    for i, t in enumerate(tracks):
        t.index = i + 1
        if t.source == "description":
            nxt = tracks[i + 1].start if i + 1 < len(tracks) else t.start
            t.end = max(t.end, nxt)
    return tracks
