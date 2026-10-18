"""Structures de données partagées."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any


def normalize(text: str | None) -> str:
    """Minuscules, sans accents, sans ponctuation ni mentions 'feat.'."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"\b(feat|ft|featuring)\.?\s.*$", "", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def fmt_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


@dataclass
class Match:
    """Résultat brut d'un service de reconnaissance pour un extrait."""

    title: str
    artist: str
    provider: str
    provider_id: str | None = None
    isrc: str | None = None
    album: str | None = None
    label: str | None = None
    year: str | None = None
    cover: str | None = None
    score: float | None = None
    links: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{normalize(self.artist)}|{normalize(self.title)}"


@dataclass
class Hit:
    """Une fenêtre d'analyse du mix (offset en secondes) et ce qui y a été reconnu."""

    offset: float
    match: Match | None


@dataclass
class VersionInfo:
    kind: str  # original | remix | mashup | edit | bootleg | vip | extended | dub | acapella | instrumental | live | cover
    label: str = ""  # ex. "Fisher Remix"
    remixer: str | None = None


@dataclass
class PlatformResult:
    platform: str
    status: str  # available | not_found | unverified | error
    url: str | None = None
    search_url: str | None = None
    matched_title: str | None = None
    same_version: bool | None = None
    note: str | None = None


@dataclass
class Track:
    index: int
    start: float
    end: float
    title: str
    artist: str
    version: VersionInfo
    hits: int = 0
    confidence: str = "low"  # high | medium | low
    source: str = "audio"  # audio | description | audio+description
    isrc: str | None = None
    album: str | None = None
    label: str | None = None
    year: str | None = None
    cover: str | None = None
    links: dict[str, str] = field(default_factory=dict)
    availability: list[PlatformResult] = field(default_factory=list)

    @property
    def start_str(self) -> str:
        return fmt_time(self.start)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start_str"] = fmt_time(self.start)
        d["end_str"] = fmt_time(self.end)
        return d
