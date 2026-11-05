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
import re
import time
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlencode, urlsplit

import httpx

from .classify import core_title, same_version
from .models import PlatformResult, Track, main_artist, normalize
from .throttle import Throttle
from .tracklist import similarity

MATCH_THRESHOLD = 0.72
CHECK_TIMEOUT = 90.0  # par plateforme et par morceau, relances comprises


def _query(t: Track) -> str:
    return f"{t.artist} {t.title}".strip()


_main_artist = main_artist  # ancien nom


def _queries(t: Track) -> list[str]:
    """Recherche complète, puis simplifiée (artiste principal + titre sans « feat. » ni crochets) :
    les moteurs de recherche des plateformes échouent souvent sur les titres à rallonge."""
    title = re.sub(r"[\[(]\s*(?:feat|ft|featuring)\.?\s[^\])]*[\])]", "", t.title or "", flags=re.I).strip()
    out = [_query(t), f"{_main_artist(t.artist)} {title}".strip(), f"{_main_artist(t.artist)} {core_title(t.title)}".strip()]
    return list(dict.fromkeys(q for q in out if q))


_Throttle = Throttle  # ancien nom, gardé pour compatibilité


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


def _clean_apple_url(url: str) -> str:
    """Retire les paramètres de suivi (mttnagencyid…) en gardant l'identifiant du morceau (?i=)."""
    parts = urlsplit(url)
    keep = {k: v[0] for k, v in parse_qs(parts.query).items() if k == "i"}
    return parts._replace(query=urlencode(keep)).geturl()


_JUNK = re.compile(r"karaok|tribute|originally (?:performed |recorded )?by|made famous|in the style of|"
                   r"backing track|cover version|lullaby|8[ -]?bit|music box", re.I)


def artist_match(a: str, b: str) -> float:
    """Ressemblance entre deux crédits d'artistes (1.0 si l'artiste principal de l'un figure dans l'autre)."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 1.0
    ma, mb = normalize(main_artist(a)), normalize(main_artist(b))
    if (ma and ma in nb) or (mb and mb in na):
        return 1.0
    return max(SequenceMatcher(None, na, nb).ratio(), SequenceMatcher(None, ma, mb).ratio())


def _best(track: Track, candidates: list[tuple[str, str, str]]) -> tuple[str, str, float] | None:
    """candidates: (artist, title, url) -> meilleur (titre affiché, url, score).

    Un titre identique ne suffit pas : l'artiste doit aussi correspondre, et les karaokés / tributes /
    reprises (« Hits 1 Karaoke - Ni**as in Paris (Originally by Jay-Z) ») sont écartés."""
    best = None
    own_junk = bool(_JUNK.search(f"{track.artist} {track.title}"))
    for artist, title, url in candidates:
        if not own_junk and _JUNK.search(f"{artist} {title}"):
            continue
        forms = [(artist, title)]
        # un titre de vidéo YouTube contient souvent "Artiste - Titre" : on teste aussi cette forme
        if not artist and " - " in title:
            a, _, ti = title.partition(" - ")
            forms.append((a, ti))
        for a, ti in forms:
            if a and track.artist and artist_match(track.artist, a) < 0.5:
                continue
            score = similarity(track.artist, track.title, a, ti)
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
    def __init__(self, client: httpx.AsyncClient | None = None, youtube: bool = True,
                 itunes_interval: float = 3.0, deezer_interval: float = 0.15) -> None:
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=True)
        self.youtube = youtube
        self._spotify_token: tuple[str, float] | None = None
        self.sc_client_id = os.environ.get("SOUNDCLOUD_CLIENT_ID")
        self._itunes = _Throttle(itunes_interval)
        self._deezer = _Throttle(deezer_interval)
        self._itunes_failures = 0  # échecs consécutifs : au-delà de 3, iTunes est considéré bloqué

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()

    async def _get_json(self, url: str, params: dict | None = None, throttle: _Throttle | None = None,
                        attempts: int = 3, **kw: Any) -> Any:
        """GET JSON avec relances quand le service limite le débit (429, 403 d'iTunes, quota Deezer)."""
        for i in range(attempts):
            if throttle:
                await throttle.wait()
            r = await self.client.get(url, params=params, **kw)
            limited = r.status_code == 429 or (r.status_code == 403 and "itunes.apple.com" in url)
            if not limited and r.status_code < 400:
                j = r.json()
                # Deezer répond 200 avec {"error": {"code": 4}} quand le quota est dépassé.
                if isinstance(j, dict) and (j.get("error") or {}).get("code") == 4:
                    limited = True
                else:
                    return j
            if not limited or i == attempts - 1:
                r.raise_for_status()
                raise httpx.HTTPStatusError(f"Limite de débit atteinte ({url})", request=r.request, response=r)
            retry_after = r.headers.get("retry-after", "")
            await asyncio.sleep(float(retry_after) if retry_after.isdigit() else 5 * 2**i)

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
        queries = ([f"isrc:{t.isrc}"] if t.isrc else []) + [f'track:"{t.title}" artist:"{t.artist}"', *_queries(t)]
        for q in queries:
            j = await self._get_json("https://api.spotify.com/v1/search", headers=headers,
                                     params={"q": q, "type": "track", "limit": 10})
            items = (j.get("tracks") or {}).get("items", [])
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
            j = await self._get_json(f"https://api.deezer.com/2.0/track/isrc:{t.isrc}", throttle=self._deezer)
            if j.get("id"):
                res = _result("deezer", t, (f"{j['artist']['name']} - {j['title']}", j["link"], 1.0), surl)
                res.isrc = j.get("isrc") or t.isrc
                return res
        queries = [f'artist:"{t.artist}" track:"{t.title}"' if t.artist else t.title, *_queries(t)]
        for q in queries:
            j = await self._get_json("https://api.deezer.com/search", params={"q": q}, throttle=self._deezer)
            items = j.get("data", [])
            found = _best(t, [(it["artist"]["name"], it["title"], it["link"]) for it in items])
            if found:
                res = _result("deezer", t, found, surl)
                res.isrc = next((it.get("isrc") for it in items if it["link"] == found[1]), None)
                return res
        return PlatformResult("deezer", "not_found", search_url=surl)

    async def deezer_popularity(self, artist: str, title: str) -> int:
        """Popularité (« rank » Deezer) du morceau, 0 s'il est introuvable : sert à départager l'original
        d'une reprise quand Shazam hésite entre les deux."""
        probe = Track(index=0, start=0, end=0, title=title, artist=artist, version=None)  # type: ignore[arg-type]
        for q in _queries(probe)[:2]:
            j = await self._get_json("https://api.deezer.com/search", params={"q": q}, throttle=self._deezer)
            items = j.get("data", [])
            found = _best(probe, [(it["artist"]["name"], it["title"], it["link"]) for it in items])
            if found:
                return next((int(it.get("rank") or 0) for it in items if it["link"] == found[1]), 0)
        return 0

    # --- Apple Music / iTunes (API publique, sans clé) ---
    async def apple_music(self, t: Track) -> PlatformResult:
        surl = search_urls(t)["apple_music"]
        if t.links.get("apple_music"):  # lien exact fourni par Shazam / AudD : inutile de chercher
            return PlatformResult("apple_music", "available", url=_clean_apple_url(t.links["apple_music"]),
                                  search_url=surl, matched_title=f"{t.artist} - {t.title}", same_version=True)
        if self._itunes_failures >= 3:
            return PlatformResult("apple_music", "error", search_url=surl,
                                  note="Apple Music limite les requêtes depuis votre connexion : réessayez plus tard")
        for q in _queries(t)[:2]:
            try:
                j = await self._get_json("https://itunes.apple.com/search", throttle=self._itunes,
                                         params={"term": q, "entity": "song", "limit": 10})
                self._itunes_failures = 0
            except httpx.HTTPError:
                self._itunes_failures += 1
                raise
            cands = [(it.get("artistName", ""), it.get("trackName", ""), it.get("trackViewUrl", ""))
                     for it in j.get("results", [])]
            found = _best(t, cands)
            if found:
                return _result("apple_music", t, found, surl)
        return PlatformResult("apple_music", "not_found", search_url=surl)

    # --- SoundCloud (beaucoup de remix / mashups / bootlegs n'existent que là) ---
    async def soundcloud(self, t: Track) -> PlatformResult:
        surl = search_urls(t)["soundcloud"]
        if not self.sc_client_id:
            return PlatformResult("soundcloud", "unverified", search_url=surl,
                                  note="Ajoutez SOUNDCLOUD_CLIENT_ID pour vérifier automatiquement")
        j = await self._get_json("https://api-v2.soundcloud.com/search/tracks",
                                 params={"q": _query(t), "client_id": self.sc_client_id, "limit": 10})
        cands = [((it.get("publisher_metadata") or {}).get("artist") or (it.get("user") or {}).get("username", ""),
                  it.get("title", ""), it.get("permalink_url", "")) for it in j.get("collection", [])]
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
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "socket_timeout": 15}) as ydl:
                return ydl.extract_info(f"ytsearch5:{_query(t)}", download=False).get("entries", [])

        entries = [e for e in (await asyncio.to_thread(run) or []) if e]
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
        results = await asyncio.gather(*(asyncio.wait_for(fn(t), CHECK_TIMEOUT) for fn in checks.values()),
                                       return_exceptions=True)
        surls = search_urls(t)
        out: list[PlatformResult] = []
        for name, res in zip(checks, results):
            if isinstance(res, asyncio.TimeoutError):
                res = PlatformResult(name, "error", search_url=surls[name], note="La plateforme ne répond pas")
            elif isinstance(res, Exception):
                res = PlatformResult(name, "error", search_url=surls[name], note=f"{type(res).__name__}: {res}"[:200])
            out.append(res)
        out.append(PlatformResult("beatport", "unverified", search_url=surls["beatport"]))
        out.append(PlatformResult("google", "unverified", search_url=surls["google"]))
        return out

