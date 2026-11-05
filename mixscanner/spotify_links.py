"""Liens Spotify de chaque morceau, sans Premium : à coller dans une playlist de l'app Spotify (Cmd+V).

L'API publique de song.link (Odesli) a été fermée en 2026 (réponse 401 PUBLIC_API_ACCESS_DEPRECATED).
Les liens sont donc cherchés, dans l'ordre :
  1. liens déjà connus (vérification Spotify, ACRCloud, AudD) ;
  2. API Spotify, si SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET sont définis ;
  3. MusicBrainz (base musicale ouverte, sans clé) : par ISRC — fourni par Shazam ou trouvé sur
     Deezer — puis par artiste + titre ;
  4. song.link, uniquement si une clé ODESLI_API_KEY est définie.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Callable

import httpx

from . import __version__
from .models import PlatformResult, Track, normalize
from .platforms import PlatformChecker, _best, _main_artist
from .throttle import Throttle

MUSICBRAINZ = "https://musicbrainz.org/ws/2"
ODESLI = "https://api.song.link/v1-alpha.1/links"
USER_AGENT = f"MixScanner/{__version__} ( https://github.com/Fulberitodesign/dj-mix-scanner )"
_TRACK_URL = re.compile(r"https://open\.spotify\.com/(?:intl-[a-z-]+/)?track/[A-Za-z0-9]+")


def known_spotify_url(t: Track) -> str | None:
    for p in t.availability:
        if p.platform == "spotify" and p.status == "available" and p.url and _TRACK_URL.match(p.url):
            return p.url
    url = t.links.get("spotify")
    return url if url and _TRACK_URL.match(url) else None


def _spotify_from_relations(relations: list[dict]) -> str | None:
    for rel in relations or []:
        m = _TRACK_URL.match((rel.get("url") or {}).get("resource", ""))
        if m:
            return m.group(0)
    return None


class MusicBrainz:
    """https://musicbrainz.org/doc/MusicBrainz_API — 1 requête par seconde, User-Agent obligatoire."""

    def __init__(self, client: httpx.AsyncClient, interval: float = 1.1) -> None:
        self.client = client
        self.throttle = Throttle(interval)

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any] | None:
        for attempt in range(3):
            await self.throttle.wait()
            r = await self.client.get(f"{MUSICBRAINZ}/{path}", params={**params, "fmt": "json"},
                                      headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            if r.status_code == 404:
                return None
            if r.status_code in (429, 503) and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        return None

    async def by_isrc(self, isrc: str) -> str | None:
        j = await self._get(f"isrc/{isrc.strip().upper()}", {"inc": "url-rels"})
        for rec in (j or {}).get("recordings", []):
            url = _spotify_from_relations(rec.get("relations"))
            if url:
                return url
        return None

    async def by_search(self, t: Track) -> str | None:
        artist = _main_artist(t.artist)
        title = t.title.replace('"', "")
        query = f'recording:"{title}"' + (f' AND artist:"{artist.replace(chr(34), "")}"' if artist else "")
        j = await self._get("recording", {"query": query, "limit": "5"})
        cands = [(" ".join(c.get("name", "") + c.get("joinphrase", "") for c in rec.get("artist-credit", [])),
                  rec.get("title", ""), rec["id"]) for rec in (j or {}).get("recordings", []) if rec.get("id")]
        found = _best(t, cands)
        if not found:
            return None
        rec = await self._get(f"recording/{found[1]}", {"inc": "url-rels+isrcs"})
        if not rec:
            return None
        url = _spotify_from_relations(rec.get("relations"))
        if url:
            return url
        for isrc in rec.get("isrcs", [])[:2]:  # un autre enregistrement du même ISRC peut avoir le lien
            url = await self.by_isrc(isrc)
            if url:
                return url
        return None


class _OdesliDisabled(Exception):
    pass


async def odesli_lookup(client: httpx.AsyncClient, source_url: str, key: str, country: str = "FR") -> str | None:
    """Lien Spotify via song.link (clé ODESLI_API_KEY requise depuis la fermeture de l'API publique)."""
    for attempt in range(3):
        r = await client.get(ODESLI, params={"url": source_url, "userCountry": country, "key": key})
        if r.status_code == 429 and attempt < 2:
            await asyncio.sleep(30)
            continue
        if r.status_code in (401, 403):
            raise _OdesliDisabled(r.text[:120])
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        return ((r.json().get("linksByPlatform") or {}).get("spotify") or {}).get("url")
    return None


async def resolve_spotify_links(
    tracks: list[Track],
    client: httpx.AsyncClient,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
    checker: PlatformChecker | None = None,
    musicbrainz: MusicBrainz | None = None,
) -> tuple[list[str], list[Track], dict[str, Any]]:
    """Renvoie (liens Spotify dans l'ordre du mix, sans doublons ; morceaux sans lien ; diagnostic).
    Les liens trouvés sont aussi enregistrés dans t.availability (badge Spotify)."""
    todo = [t for t in tracks if t.title and normalize(t.title) != "id"]
    checker = checker or PlatformChecker(client, youtube=False)
    mb = musicbrainz or MusicBrainz(client)
    odesli_key = os.environ.get("ODESLI_API_KEY")
    spotify_api = bool(os.environ.get("SPOTIFY_CLIENT_ID") and os.environ.get("SPOTIFY_CLIENT_SECRET"))
    links: list[str] = []
    missing: list[Track] = []
    diag: dict[str, Any] = {"found_by": {}, "not_found": 0, "errors": 0, "first_error": None}

    def error(e: Exception) -> None:
        diag["errors"] += 1
        diag["first_error"] = diag["first_error"] or f"{type(e).__name__}: {e}"[:200]

    async def find(t: Track) -> tuple[str | None, str]:
        nonlocal odesli_key
        url = known_spotify_url(t)
        if url:
            return url, "déjà connu"
        if spotify_api:
            try:
                res = await checker.spotify(t)
                if res.status == "available" and res.url and res.same_version is not False:
                    return res.url, "API Spotify"
            except Exception as e:
                error(e)
        isrcs = [t.isrc] if t.isrc else []
        deezer = next((p for p in t.availability if p.platform == "deezer" and p.status == "available"), None)
        if not isrcs:
            try:
                found = deezer if deezer and deezer.isrc else await checker.deezer(t)
                if found.status == "available":
                    deezer = found
                    if found.isrc and found.same_version is not False:
                        isrcs.append(found.isrc)
            except Exception as e:
                error(e)
        for isrc in isrcs:
            try:
                url = await mb.by_isrc(isrc)
                if url:
                    return url, "MusicBrainz"
            except Exception as e:
                error(e)
        try:
            url = await mb.by_search(t)
            if url:
                return url, "MusicBrainz"
        except Exception as e:
            error(e)
        source = deezer.url if deezer and deezer.status == "available" else t.links.get("apple_music")
        if odesli_key and source:
            try:
                url = await odesli_lookup(client, source, odesli_key)
                if url:
                    return url, "song.link"
            except _OdesliDisabled as e:
                odesli_key = None  # clé refusée : inutile de réessayer pour chaque morceau
                error(RuntimeError(f"song.link a refusé la clé ODESLI_API_KEY ({e})"))
            except Exception as e:
                error(e)
        return None, ""

    for i, t in enumerate(todo):
        url, how = await find(t)
        if url:
            diag["found_by"][how] = diag["found_by"].get(how, 0) + 1
            if not known_spotify_url(t):
                t.availability = [p for p in t.availability if p.platform != "spotify"]
                t.availability.insert(0, PlatformResult("spotify", "available", url=url,
                                                        matched_title=f"{t.artist} - {t.title}",
                                                        same_version=True, note=f"via {how}"))
            if url not in links:
                links.append(url)
        else:
            diag["not_found"] += 1
            missing.append(t)
        on_progress(i + 1, len(todo))
    return links, missing, diag
