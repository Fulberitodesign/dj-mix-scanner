"""Détection du type de version d'un morceau : original, remix, mashup, edit..."""
from __future__ import annotations

import re

from .models import VersionInfo, normalize

# Ordre = priorité : le premier motif trouvé l'emporte.
_KINDS: list[tuple[str, str]] = [
    ("mashup", r"mash[\s-]?up|\bvs\.?\s"),
    ("bootleg", r"bootleg"),
    ("vip", r"\bvip\b"),
    ("acapella", r"a[\s-]?cap+ell?a"),
    ("instrumental", r"instrumental"),
    ("remix", r"remix|\brmx\b|\bflip\b|re-?work|re-?fix|remake|\brework\b"),
    ("edit", r"(?<!radio )(?<!single )\bre-?edit\b|(?<!radio )(?<!single )\bedit\b"),
    ("dub", r"\bdub\b(?! ?step)"),
    ("extended", r"extended|club mix"),
    ("live", r"\blive\b"),
    ("cover", r"\bcover\b"),
]

# Parties "version" : (...) / [...] / " - xxx Remix" en fin de titre.
_PARTS = re.compile(r"\(([^)]*)\)|\[([^\]]*)\]|\s[-–—]\s([^-–—]+)$")
_REMIXER = re.compile(
    r"^(.*?)\s+(?:remix|rmx|flip|re-?work|re-?edit|edit|bootleg|vip|mashup|refix|remake|dub)\b",
    re.I,
)

# « JPTheDJ Tech House Remix » : le genre (ou l'année) n'est pas le nom du remixeur.
_GENRE_TAIL = re.compile(
    r"(?:\s*\b(?:afro|tech|deep|progressive|melodic|latin|bass|future|tropical|organic|minimal|hard|"
    r"house|techno|trance|techengue|reggaeton|dembow|amapiano|edm|dnb|drum\s*(?:and|&|n)\s*bass|dubstep|"
    r"trap|garage|ukg|disco|funk|club|festival|extended|radio|original|summer|\d{4}))+\s*$",
    re.I,
)

KIND_LABELS_FR = {
    "original": "Original",
    "remix": "Remix",
    "mashup": "Mashup",
    "edit": "Edit",
    "bootleg": "Bootleg",
    "vip": "VIP",
    "extended": "Version longue",
    "dub": "Dub",
    "acapella": "A cappella",
    "instrumental": "Instrumental",
    "live": "Live",
    "cover": "Reprise",
}


def version_parts(title: str) -> list[str]:
    return [next(g for g in m.groups() if g is not None).strip() for m in _PARTS.finditer(title or "")]


def core_title(title: str) -> str:
    """Titre sans les mentions de version, normalisé (sert à comparer deux titres)."""
    t = _PARTS.sub(" ", title or "")
    return normalize(t)


def classify(title: str, artist: str = "") -> VersionInfo:
    parts = version_parts(title)
    haystack = " | ".join(parts).lower()
    whole = f"{title} {artist}".lower()

    for kind, pattern in _KINDS:
        found_in_part = next((p for p in parts if re.search(pattern, p, re.I)), None)
        # Un mashup est souvent indiqué dans le champ artiste ("A vs B").
        if found_in_part or (kind == "mashup" and re.search(pattern, whole, re.I)):
            label = found_in_part or ""
            # « Medusa (Remix) », « Pop, Lock & Drop It (Acappella) » : le libellé ne dit rien de plus que le type
            if re.fullmatch(pattern + r"|" + re.escape(normalize(KIND_LABELS_FR.get(kind, ""))) + r"|rmx",
                            normalize(label), re.I):
                label = ""  # « Medusa (Remix) » : le libellé n'apporte rien de plus que le type
            remixer = None
            m = _REMIXER.match(label)
            if m:
                remixer = _GENRE_TAIL.sub("", m.group(1)).strip(" -–") or None
            return VersionInfo(kind=kind, label=label, remixer=remixer)

    label = next((p for p in parts if re.search(r"original|radio|single", p, re.I)), "")
    if not haystack:
        label = ""
    return VersionInfo(kind="original", label=label)


def same_version(title_a: str, title_b: str) -> bool:
    """Vrai si deux titres désignent la même version (même type, même remixeur)."""
    a, b = classify(title_a), classify(title_b)
    if a.kind != b.kind:
        # "Original Mix", "Extended Mix" et le titre nu sont la même œuvre.
        return {a.kind, b.kind} <= {"original", "extended"}
    if a.remixer and b.remixer:
        return normalize(a.remixer) == normalize(b.remixer)
    return True
