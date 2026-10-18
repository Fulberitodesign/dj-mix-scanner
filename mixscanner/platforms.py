"""Vérifie la disponibilité de chaque morceau sur les plateformes (Spotify, SoundCloud, Deezer,
Apple Music, YouTube, Beatport, Google) et précise si c'est la même version (original/remix/mashup).

Statuts :
  available   -> trouvé et confirmé par l'API de la plateforme
  not_found   -> recherche faite, rien de correspondant
  unverified  -> pas d'API accessible (clé absente) : un lien de recherche est fourni
  error       -> la plateforme n'a pas répondu
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
from urllib.parse import quote_plus

import httpx

from .classify import same_version
from .models import PlatformResult, Track, normalize
from .tracklist import similarity

MATCH_THRESHOLD = 0.72


def _query(t: Track) -> str:
    return f"{t.artist} {t.title}".strip()


def search_urls(t: Track) -> dict[str, str]:
    q = quote_plus(_query(t))
    return {
        "spotify": f"https://open.spotify.com/search/{quote_plus(_query(t))}",
        "soundcloud": f"https://soundcloud.com/search/sounds?q={q}",
        "deezer": f"https://www.deezer.com/search/{q}",
        "apple_music": f"https://music.apple.com/search?term={q}",
        "youtube": f"https://www.youtube.com/results?search_query={q}",
        "beatport": f"https://www.beatport.com/search?q={q}",
        "google": f"https://www.google.com/search?q={q}",
    }


def _best(track: Track, candidates: list[tuple[str, str, str]]) -> tuple[str, str, float] | None:
    """candidates: (artist, title, url) -> meilleur (titre affiché, url, score)."""
    best = None
    for artist, title, url in candidates:
        score = similarity(track.artist, track.title, artist, title)
        # un titre de vidéo YouTube contient souvent "Artiste - Titre" : on teste aussi cette forme
        if not artist and " - " in title:
            a, _, ti = title.partition(" - ")
            score = max(score, similarity(track.artist, track.title, a, ti))
        if best is None or score > best[2]:
            best = (f"{artist} - {title}" if artist else title, url, score)
    return best if best and best[2] >= MATCH_THRESHOLD else None


def _result(platform: str, track: Track, found, search_url: str) -> PlatformResult:
    if not found:
        return PlatformResult(platform, "not_found", search_url=search_url)
    label, url, _ = found
    title_only = label.split(" - ", 1)[-1]
    return PlatformResult(platform, "available", url=url, search_url=search_url, matched_title=label,
                          same_version=same_version(track.title, title_only))


class PlatformChecker:
    def __init__(self, client: httpx.AsyncClient | None = None, youtube: bool = True) -> None:
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=True)
        self.youtube = youtube
        self._spotify_token: tuple[str, float] | None = None
        self.sc_client_id = os.environ.get("SOUNDCLOUD_CLIENT_ID")

    # --- Spotify (clé gratuite : https://developer.spotify.com/dashboard) ---
    async def _spotify_auth(self) -> str | None:
        cid, secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
        if not cid or not secret:
            return None
        if self._spotify_token and self._spotify_token[1] > time.time() + 30:
            return self._spotify_token[0]
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        r = await self.client.post("https://accounts.spotify.com/api/token",
                                   data={"grant_type": "client_credentials"},
                                   headers={"Authorization": f"Basic {basic}"})
        r.raise_for_status()
        j = r.json()
        self._spotify_token = (j["access_token"], time.time() + j.get("expires_in", 3600))
        return self._spotify_token[0]

    async def spotify(self, t: Track) -> PlatformResult:
        surl = search_urls(t)["spotify"]
        token = await self._spotify_auth()
        if not token:
            if t.links.get("spotify"):
                return PlatformResult("spotify", "available", url=t.links["spotify"], search_url=surl,
                                      matched_title=f"{t.artist} - {t.title}", same_version=True)
            return PlatformResult("spotify", "unverified", search_url=surl,
                                  note="Ajoutez SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET pour vérifier")
        headers = {"Authorization": f"Bearer {token}"}
        queries = ([f"isrc:{t.isrc}"] if t.isrc else []) + [f'track:"{t.title}" artist:"{t.artist}"', _query(t)]
        for q in queries:
            r = await self.client.get("https://api.spotify.com/v1/search", headers=headers,
                                      params={"q": q, "type": "track", "limit": 10})
            r.raise_for_status()
            items = r.json().get("tracks", {}).get("items", [])
            cands = [(", ".join(a["name"] for a in it["artists"]), it["name"], it["external_urls"]["spotify"])
                     for it in items]
            found = _best(t, cands)
            if found:
                return _result("spotify", t, found, surl)
        return PlatformResult("spotify", "not_found", search_url=surl)

    # --- Deezer (API publique, sans clé) ---
    async def deezer(self, t: Track) -> PlatformResult:
        surl = search_urls(t)["deezer"]
        if t.isrc:
            r = await self.client.get(f"https://api.deezer.com/2.0/track/isrc:{t.isrc}")
            j = r.json()
            if r.status_code == 200 and j.get("id"):
                return _result("deezer", t, (f"{j['artist']['name']} - {j['title']}", j["link"], 1.0), surl)
        r = await self.client.get("https://api.deezer.com/search",
                                  params={"q": f'artist:"{t.artist}" track:"{t.title}"' if t.artist else t.title})
        r.raise_for_status()
        items = r.json().get("data", [])
        if not items:
            r = await self.client.get("https://api.deezer.com/search", params={"q": _query(t)})
            items = r.json().get("data", [])
        cands = [(it["artist"]["name"], it["title"], it["link"]) for it in items]
        return _result("deezer", t, _best(t, cands), surl)

    # --- Apple Music / iTunes (API publique, sans clé) ---
    async def apple_music(self, t: Track) -> PlatformResult:
        surl = search_urls(t)["apple_music"]
        r = await self.client.get("https://itunes.apple.com/search",
                                  params={"term": _query(t), "entity": "song", "limit": 10})
        r.raise_for_status()
        cands = [(it.get("artistName", ""), it.get("trackName", ""), it.get("trackViewUrl", ""))
                 for it in r.json().get("results", [])]
        return _result("apple_music", t, _best(t, cands), surl)

    # --- SoundCloud (beaucoup de remix / mashups / bootlegs n'existent que là) ---
    async def soundcloud(self, t: Track) -> PlatformResult:
        surl = search_urls(t)["soundcloud"]
        if not self.sc_client_id:
            return PlatformResult("soundcloud", "unverified", search_url=surl,
                                  note="Ajoutez SOUNDCLOUD_CLIENT_ID pour vérifier automatiquement")
        r = await self.client.get("https://api-v2.soundcloud.com/search/tracks",
                                  params={"q": _query(t), "client_id": self.sc_client_id, "limit": 10})
        r.raise_for_status()
        cands = [((it.get("publisher_metadata") or {}).get("artist") or (it.get("user") or {}).get("username", ""),
                  it.get("title", ""), it.get("permalink_url", "")) for it in r.json().get("collection", [])]
        return _result("soundcloud", t, _best(t, cands), surl)

    # --- YouTube (recherche via yt-dlp, sans clé) ---
    async def youtube_search(self, t: Track) -> PlatformResult:
        surl = search_urls(t)["youtube"]
        if t.links.get("youtube"):
            return PlatformResult("youtube", "available", url=t.links["youtube"], search_url=surl,
                                  matched_title=f"{t.artist} - {t.title}", same_version=True)
        if not self.youtube:
            return PlatformResult("youtube", "unverified", search_url=surl)

        def run():
            import yt_dlp
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as ydl:
                return ydl.extract_info(f"ytsearch5:{_query(t)}", download=False).get("entries", [])

        entries = await asyncio.to_thread(run)
        cands = [("", e.get("title", ""), e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}")
                 for e in entries]
        return _result("youtube", t, _best(t, cands), surl)

    async def check(self, t: Track) -> list[PlatformResult]:
        if not t.title or normalize(t.title) == "id":
            return []
        checks = {
            "spotify": self.spotify, "soundcloud": self.soundcloud, "deezer": self.deezer,
            "apple_music": self.apple_music, "youtube": self.youtube_search,
        }
        results = await asyncio.gather(*(fn(t) for fn in checks.values()), return_exceptions=True)
        surls = search_urls(t)
        out: list[PlatformResult] = []
        for name, res in zip(checks, results):
            if isinstance(res, Exception):
                res = PlatformResult(name, "error", search_url=surls[name], note=str(res)[:200])
            out.append(res)
        out.append(PlatformResult("beatport", "unverified", search_url=surls["beatport"]))
        out.append(PlatformResult("google", "unverified", search_url=surls["google"]))
        return out

