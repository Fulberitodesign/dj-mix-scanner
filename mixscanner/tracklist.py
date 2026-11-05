"""Construction de la tracklist chronologique à partir des fenêtres reconnues,
et lecture d'une éventuelle tracklist dans la description / les chapitres YouTube."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
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
    alternatives: list[Match] = field(default_factory=list)


_ARTICLES = re.compile(r"^(?:the|a|an|la|le|les|el|los|las|l)\s+")


def _title_core(title: str) -> str:
    return _ARTICLES.sub("", core_title(title))


def same_song_title(a: str, b: str, threshold: float = 0.85) -> bool:
    """« The Box » / « A Box », « Taki Taki » / « Taki Tanki (feat. …) », « Last Last » / « Last Last (Afro Dancehall) »."""
    ta, tb = _title_core(a), _title_core(b)
    return bool(ta and tb) and SequenceMatcher(None, ta, tb).ratio() >= threshold


def _merge_lookalikes(runs: list[_Run], gap: float) -> list[_Run]:
    """Deux détections voisines au titre presque identique mais d'artistes différents sont presque
    toujours le même passage : Shazam hésite entre l'original et une reprise / un remake / un remix.
    On n'en garde qu'une (la plus détectée) et l'autre est conservée comme « autre détection »."""
    out: list[_Run] = []
    for r in runs:
        prev = out[-1] if out else None
        if (prev and prev.key != r.key and r.first - prev.last <= gap and min(prev.count, r.count) == 1
                and same_song_title(prev.match.title, r.match.title)):
            keep, other = (prev, r) if prev.count >= r.count else (r, prev)
            keep.alternatives = [*keep.alternatives, other.match, *other.alternatives]
            keep.first, keep.last = min(prev.first, r.first), max(prev.last, r.last)
            keep.count = prev.count + r.count
            out[-1] = keep
        else:
            out.append(r)
    return out


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
        if h.match is None or not (h.match.title or h.match.artist):
            continue
        key = h.match.key
        if runs and runs[-1].key == key and h.offset - runs[-1].last <= max_gap:
            runs[-1].last = h.offset
            runs[-1].count += 1
            _enrich(runs[-1].match, h.match)
        else:
            # copie : les détections d'origine (cache, affichage en direct) ne doivent pas être modifiées
            runs.append(_Run(key, replace(h.match, links=dict(h.match.links)), h.offset, h.offset, 1))

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

    runs = _merge_lookalikes(runs, gap=min(max_gap, 90.0))
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
            confidence=_confidence(r.count - len(r.alternatives)),
            isrc=m.isrc, album=m.album, label=m.label, year=m.year, cover=m.cover, links=dict(m.links),
            alternatives=list(r.alternatives),
        ))
    return tracks


def _confidence(hits: int) -> str:
    return "high" if hits >= 3 else "medium" if hits == 2 else "low"


def use_match(t: Track, m: Match) -> None:
    """Remplace la détection retenue pour un morceau par une autre (garde l'ancienne en alternative)."""
    current = Match(title=t.title, artist=t.artist, provider="shazam", isrc=t.isrc, album=t.album,
                    label=t.label, year=t.year, cover=t.cover, links=dict(t.links))
    t.alternatives = [current, *(a for a in t.alternatives if a is not m)]
    t.title, t.artist, t.version = m.title, m.artist, classify(m.title, m.artist)
    t.isrc, t.album, t.label, t.year, t.cover, t.links = m.isrc, m.album, m.label, m.year, m.cover, dict(m.links)


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
        text = re.sub(r"^\s*\d{1,3}\s*[.)]\s+", "", text)  # « [00:00] 01. Artiste - Titre »
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


_MASHUP_SEP = re.compile(r"\s+(?:vs\.?|x)\s+", re.I)


def _mashup_parts(title: str) -> list[str]:
    """« Who's That Girl vs. SECRETO VICTORIA (Lara Mashup) » -> ["who s that girl", "secreto victoria"]."""
    parts = [core_title(p) for p in _MASHUP_SEP.split(title or "")]
    parts = [p for p in parts if p]
    return parts if len(parts) >= 2 else []


def _absorb_mashup(tracks: list[Track], used: set[int], e: DescEntry, window: float) -> bool:
    """Un mashup listé par l'uploader est souvent reconnu à l'écoute comme ses morceaux d'origine,
    détectés séparément : on les regroupe en un seul morceau, avec le nom donné par l'uploader."""
    parts = _mashup_parts(e.title)
    if not parts or classify(e.title, e.artist).kind != "mashup":
        return False
    comps = [i for i, t in enumerate(tracks)
             if i not in used and t.source != "description" and abs(t.start - e.start) <= window
             and max(SequenceMatcher(None, p, core_title(t.title)).ratio() for p in parts) >= 0.7]
    if not comps:
        return False
    first = tracks[comps[0]]
    group = [tracks[i] for i in comps]
    first.start = min(t.start for t in group)
    first.end = max(t.end for t in group)
    first.hits = sum(t.hits for t in group)
    first.title, first.artist = e.title, e.artist
    first.version = classify(e.title, e.artist)
    first.source, first.confidence = "audio+description", "high"
    # L'ISRC et les liens sont ceux d'un morceau d'origine, pas du mashup : on les retire.
    first.isrc = first.album = first.label = first.year = None
    first.links = {}
    used.update(comps)
    for i in comps[1:]:
        tracks[i].index = -1  # supprimé plus bas
    return True


def merge_with_description(tracks: list[Track], entries: list[DescEntry], window: float = 240.0) -> list[Track]:
    """Croise la détection audio avec la tracklist de l'uploader.

    Les morceaux confirmés des deux côtés passent en source "audio+description" ;
    ceux listés par l'uploader mais non reconnus à l'écoute sont ajoutés (source "description").
    """
    used: set[int] = set()
    for e in entries:
        if normalize(e.title) in ("id", "") or normalize(e.artist) == "id":
            continue
        if _absorb_mashup(tracks, used, e, window):
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

    tracks = [t for t in tracks if t.index != -1]
    tracks.sort(key=lambda t: t.start)
    for i, t in enumerate(tracks):
        t.index = i + 1
        if t.source == "description":
            nxt = tracks[i + 1].start if i + 1 < len(tracks) else t.start
            t.end = max(t.end, nxt)
    return tracks
