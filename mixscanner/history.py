"""Historique des analyses : chaque analyse terminée est enregistrée sur le Mac (un fichier JSON par
mix, dans le dossier du cache) et peut être rouverte sans refaire le scan, ou supprimée."""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .audio import default_cache_dir
from .export import to_playlist
from .pipeline import ScanResult

_ID = re.compile(r"^[0-9a-f]{12}$")


def history_dir() -> Path:
    return default_cache_dir() / "historique"


def source_kind(url: str | None) -> str:
    u = (url or "").lower()
    if "youtu" in u:
        return "YouTube"
    if "soundcloud" in u:
        return "SoundCloud"
    if "mixcloud" in u:
        return "Mixcloud"
    if "://" not in u:
        return "Fichier"
    return "Lien"


def summary(res: ScanResult) -> dict[str, Any]:
    """Chiffres affichés en haut de l'analyse et dans l'historique."""
    tracks = res.tracks
    streaming = sum(1 for t in tracks if any(p.status == "available" and p.platform in ("spotify", "deezer", "apple_music")
                                             for p in t.availability))
    lines = to_playlist(res).strip()
    return {
        "tracks": len(tracks),
        "review": sum(1 for t in tracks if t.needs_review),
        "streaming": streaming,
        "checked": any(t.availability for t in tracks),
        "playlist": len(lines.splitlines()) if lines else 0,
        "windows": res.windows,
        "recognized_windows": res.recognized_windows,
        "coverage": round(100 * res.recognized_windows / res.windows) if res.windows else None,
    }


def _path(entry_id: str) -> Path:
    if not _ID.match(entry_id or ""):
        raise KeyError(entry_id)
    return history_dir() / f"{entry_id}.json"


def save(res: ScanResult, url: str, entry_id: str | None = None, options: dict | None = None,
         created: float | None = None) -> str:
    """Enregistre (ou remplace) une analyse ; renvoie son identifiant. `created` garde la date d'origine
    (simple correction d'un morceau) ; sinon c'est une nouvelle analyse, datée de maintenant."""
    entry_id = entry_id or uuid.uuid4().hex[:12]
    path = _path(entry_id)
    created = created or time.time()
    data = {
        "id": entry_id, "created": created, "updated": time.time(), "url": url,
        "title": res.source.get("title") or url, "kind": source_kind(res.source.get("url") or url),
        "duration": res.source.get("duration"), "options": options or {},
        "summary": summary(res), "result": res.to_dict(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)
    return entry_id


def entries() -> list[dict[str, Any]]:
    """Analyses enregistrées, de la plus récente à la plus ancienne (sans les morceaux)."""
    out = []
    folder = history_dir()
    if not folder.exists():
        return out
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        data.pop("result", None)
        out.append(data)
    out.sort(key=lambda d: d.get("created") or 0, reverse=True)
    return out


def find_same(url: str, options: dict | None) -> str | None:
    """Analyse déjà enregistrée du même mix avec les mêmes réglages (relancer la remplace)."""
    for e in entries():
        if e.get("url") == url and (e.get("options") or {}) == (options or {}):
            return e["id"]
    return None


def load(entry_id: str) -> tuple[ScanResult, dict[str, Any]]:
    data = json.loads(_path(entry_id).read_text())
    return ScanResult.from_dict(data["result"]), data


def delete(entry_id: str) -> bool:
    try:
        _path(entry_id).unlink()
        return True
    except (FileNotFoundError, KeyError):
        return False
