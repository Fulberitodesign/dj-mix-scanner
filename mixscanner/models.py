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


_ARTIST_SEP = re.compile(r"\s*,\s*|\s+(?:&|x|×|\+)\s+|\s+(?i:vs\.?|feat\.?|ft\.?|featuring|with)\s+")


def main_artist(artist: str | None) -> str:
    """Premier artiste crédité : « Kanye West & Weezer » -> « Kanye West », « F.L.Y. - Fast Life Yungstaz »
    -> « F.L.Y. », « Tahara (Br) » -> « Tahara ». « Lil Nas X » reste intact (x seul, minuscule, entre espaces)."""
    a = (artist or "").split(" - ", 1)[0]
    a = re.sub(r"\s*[(\[][^)\]]*[)\]]", "", a)
    return _ARTIST_SEP.split(a, maxsplit=1)[0].strip()


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
    isrc: str | None = None


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
    # Autres morceaux reconnus au même endroit avec un titre presque identique : souvent une reprise
    # ou un « remake » que Shazam a confondu avec l'original (ex. « Drxse - A Box » / « Roddy Ricch - The Box »).
    alternatives: list[Match] = field(default_factory=list)

    @property
    def start_str(self) -> str:
        return fmt_time(self.start)

    @property
    def ambiguous(self) -> bool:
        """Plusieurs versions détectées une seule fois chacune : impossible de savoir laquelle est la bonne."""
        return bool(self.alternatives) and self.hits <= len(self.alternatives) + 1

    @property
    def needs_review(self) -> bool:
        """Détection douteuse, à vérifier à l'écoute."""
        if self.source == "description":
            return False
        if self.ambiguous:
            return True
        av = {p.platform: p.status for p in self.availability}
        # Introuvable sur Deezer ET Apple Music alors que la recherche a fonctionné : version copiée,
        # inédit ou erreur de reconnaissance.
        return self.hits <= 1 and av.get("deezer") == "not_found" and av.get("apple_music") == "not_found"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start_str"] = fmt_time(self.start)
        d["end_str"] = fmt_time(self.end)
        d["needs_review"] = self.needs_review
        return d
