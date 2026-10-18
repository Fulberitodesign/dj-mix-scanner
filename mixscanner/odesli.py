"""Liens Spotify sans clé ni Premium, via Odesli (song.link) : à partir d'un lien Deezer, Apple Music
ou YouTube du morceau, Odesli renvoie le lien du même morceau sur Spotify.

Ces liens se collent directement dans une playlist de l'app Spotify (Cmd+V) : c'est la façon la plus
fiable de créer la playlist sans passer par une app tierce. Odesli limite à ~10 requêtes/minute.
"""
from __future__ import annotations

import asyncio
from typing import Callable

import httpx

from .models import PlatformResult, Track, normalize

API = "https://api.song.link/v1-alpha.1/links"
_SOURCES = ("deezer", "apple_music", "youtube", "soundcloud")


def _spotify_url(t: Track) -> str | None:
    for p in t.availability:
        if p.platform == "spotify" and p.status == "available" and p.url and "/track/" in p.url:
            return p.url
    return None


def _source_url(t: Track) -> str | None:
    """Un lien du morceau sur une autre plateforme, pour l'envoyer à Odesli."""
    found = {p.platform: p.url for p in t.availability if p.status == "available" and p.url}
    for name in _SOURCES:
        if found.get(name):
            return found[name]
        if t.links.get(name):
            return t.links[name]
    return None


async def lookup(client: httpx.AsyncClient, source_url: str, country: str = "FR") -> str | None:
    for attempt in range(3):
        r = await client.get(API, params={"url": source_url, "userCountry": country})
        if r.status_code == 429:  # limite de débit : on attend une minute
            await asyncio.sleep(60 if attempt < 2 else 0)
            continue
        if r.status_code != 200:
            return None
        return ((r.json().get("linksByPlatform") or {}).get("spotify") or {}).get("url")
    return None


async def resolve_spotify_links(
    tracks: list[Track],
    client: httpx.AsyncClient,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
    delay: float = 6.5,
) -> tuple[list[str], list[Track]]:
    """Renvoie (liens Spotify dans l'ordre du mix, sans doublons ; morceaux sans lien trouvé).
    Les liens trouvés sont aussi enregistrés dans t.availability (badge Spotify)."""
    todo = [t for t in tracks if t.title and normalize(t.title) != "id"]
    links: list[str] = []
    missing: list[Track] = []
    for i, t in enumerate(todo):
        url = _spotify_url(t)
        if not url and (src := _source_url(t)):
            try:
                url = await lookup(client, src)
            except httpx.HTTPError:
                url = None
            if url:
                t.availability = [p for p in t.availability if p.platform != "spotify"]
                t.availability.insert(0, PlatformResult("spotify", "available", url=url,
                                                        matched_title=f"{t.artist} - {t.title}",
                                                        same_version=True, note="via song.link"))
            if i + 1 < len(todo):
                await asyncio.sleep(delay)
        if url:
            if url not in links:
                links.append(url)
        else:
            missing.append(t)
        on_progress(i + 1, len(todo))
    return links, missing
