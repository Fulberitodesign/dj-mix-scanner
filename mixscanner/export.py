"""Exports de la tracklist : texte, CSV, JSON, Markdown."""
from __future__ import annotations

import csv
import io
import json

from .classify import KIND_LABELS_FR
from .models import fmt_time
from .pipeline import ScanResult

PLATFORM_NAMES = {
    "spotify": "Spotify", "soundcloud": "SoundCloud", "deezer": "Deezer", "apple_music": "Apple Music",
    "youtube": "YouTube", "beatport": "Beatport", "google": "Google",
}
_STATUS = {"available": "✅", "not_found": "❌", "unverified": "🔎", "error": "⚠️"}


def _version(t) -> str:
    kind = KIND_LABELS_FR.get(t.version.kind, t.version.kind)
    return f"{kind} ({t.version.label})" if t.version.label and t.version.kind != "original" else kind


def _platform_cell(p) -> str:
    s = _STATUS.get(p.status, "?")
    if p.status == "available" and p.same_version is False:
        s += " (autre version)"
    return s


def to_text(res: ScanResult) -> str:
    lines = [f"{res.source['title']}", res.source.get("url") or "", ""]
    for t in res.tracks:
        name = f"{t.artist} - {t.title}" if t.artist else t.title
        lines.append(f"{fmt_time(t.start):>8}  {name}  [{_version(t)}]")
    return "\n".join(lines) + "\n"


def to_csv(res: ScanResult) -> str:
    buf = io.StringIO()
    platforms = list(PLATFORM_NAMES)
    w = csv.writer(buf)
    w.writerow(["#", "debut", "fin", "artiste", "titre", "version", "confiance", "source", "isrc", "label",
                *[f"{p}_statut" for p in platforms], *[f"{p}_lien" for p in platforms]])
    for t in res.tracks:
        av = {p.platform: p for p in t.availability}
        w.writerow([
            t.index, fmt_time(t.start), fmt_time(t.end), t.artist, t.title, _version(t), t.confidence,
            t.source, t.isrc or "", t.label or "",
            *[av[p].status if p in av else "" for p in platforms],
            *[(av[p].url or av[p].search_url or "") if p in av else "" for p in platforms],
        ])
    return buf.getvalue()


def to_json(res: ScanResult) -> str:
    return json.dumps(res.to_dict(), ensure_ascii=False, indent=2)


def to_markdown(res: ScanResult) -> str:
    out = [f"# {res.source['title']}", "", f"Source : {res.source.get('url', '')}", "",
           "| # | Début | Morceau | Version | " + " | ".join(PLATFORM_NAMES.values()) + " |",
           "|---|---|---|---|" + "---|" * len(PLATFORM_NAMES)]
    for t in res.tracks:
        av = {p.platform: p for p in t.availability}
        cells = []
        for key in PLATFORM_NAMES:
            p = av.get(key)
            if not p:
                cells.append("")
                continue
            url = p.url or p.search_url
            cells.append(f"[{_platform_cell(p)}]({url})" if url else _platform_cell(p))
        name = f"{t.artist} - {t.title}" if t.artist else t.title
        out.append(f"| {t.index} | {fmt_time(t.start)} | {name} | {_version(t)} | " + " | ".join(cells) + " |")
    out += ["", "✅ disponible · ❌ introuvable · 🔎 lien de recherche (non vérifié) · ⚠️ erreur"]
    return "\n".join(out) + "\n"


EXPORTERS = {"txt": to_text, "csv": to_csv, "json": to_json, "md": to_markdown}
