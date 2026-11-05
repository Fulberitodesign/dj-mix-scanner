"""Exports de la tracklist : texte, CSV, JSON, Markdown."""
from __future__ import annotations

import csv
import io
import json
import re

from .classify import KIND_LABELS_FR
from .models import fmt_time, main_artist, normalize
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
                *[f"{p}_statut" for p in platforms], *[f"{p}_lien" for p in platforms],
                "a_verifier", "autres_detections"])
    for t in res.tracks:
        av = {p.platform: p for p in t.availability}
        w.writerow([
            t.index, fmt_time(t.start), fmt_time(t.end), t.artist, t.title, _version(t), t.confidence,
            t.source, t.isrc or "", t.label or "",
            *[av[p].status if p in av else "" for p in platforms],
            *[(av[p].url or av[p].search_url or "") if p in av else "" for p in platforms],
            "oui" if t.needs_review else "", " / ".join(f"{a.artist} - {a.title}" for a in t.alternatives),
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


_FEAT = re.compile(r"\s*[(\[]\s*(?:feat|ft|featuring|with)\.?\s[^)\]]*[)\]]", re.I)
_NOISE = re.compile(r"\s*[(\[]\s*(?:mixed|original mix|clean|explicit|dirty|radio edit|single version)\s*[)\]]", re.I)


def clean_title(title: str) -> str:
    """Titre prêt pour une recherche Spotify : sans « (feat. …) », « [Mixed] », « (Original Mix) ».
    Les mots censurés (« Ni**as ») sont gardés : Spotify les écrit souvent ainsi ; le titre officiel
    trouvé sur Deezer, non censuré, est utilisé en priorité quand il existe."""
    t = _NOISE.sub("", _FEAT.sub("", title or ""))
    return re.sub(r"\s+", " ", t).strip(" -–")


def _catalog_name(t) -> tuple[str, str] | None:
    """Nom officiel du morceau trouvé sur Deezer (non censuré, artiste principal correct), sinon Apple Music,
    seulement si c'est la même version que celle jouée dans le mix."""
    for platform in ("deezer", "apple_music"):
        p = next((p for p in t.availability if p.platform == platform), None)
        if not p or p.status != "available" or p.same_version is False or not p.matched_title:
            continue
        if p.matched_title == f"{t.artist} - {t.title}":  # simple copie de la détection, pas un nom officiel
            continue
        artist, sep, title = p.matched_title.partition(" - ")
        if sep and "*" not in title:
            return artist, title
    return None


def playlist_line(t) -> str:
    """Une ligne « Artiste - Titre » que Spotlistr / TuneMyMusic retrouvent facilement sur Spotify."""
    artist, title = _catalog_name(t) or (t.artist, t.title)
    artist, title = main_artist(artist) or artist, clean_title(title) or title
    return f"{artist} - {title}" if artist else title


def to_playlist(res: ScanResult) -> str:
    """Une ligne « Artiste - Titre » par morceau (sans doublons ni « ID »), à coller dans
    Spotlistr / TuneMyMusic / Soundiiz pour créer la playlist."""
    lines: dict[str, str] = {}
    for t in res.tracks:
        if not t.title or normalize(t.title) == "id":
            continue
        line = playlist_line(t)
        lines.setdefault(normalize(line), line)
    return "\n".join(lines.values()) + "\n"


def to_review(res: ScanResult) -> str:
    """Morceaux à vérifier à l'écoute (détection douteuse), avec les autres versions détectées."""
    out = []
    for t in res.tracks:
        if not t.needs_review:
            continue
        line = f"{fmt_time(t.start):>8}  {t.artist} - {t.title}"
        if t.alternatives:
            line += "   (ou : " + " / ".join(f"{a.artist} - {a.title}" for a in t.alternatives) + ")"
        out.append(line)
    return "\n".join(out) + ("\n" if out else "")


EXPORTERS = {"txt": to_text, "csv": to_csv, "json": to_json, "md": to_markdown, "playlist": to_playlist,
             "review": to_review}
