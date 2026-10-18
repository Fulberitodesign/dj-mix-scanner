"""Création d'une playlist Spotify à partir de la tracklist (connexion OAuth avec PKCE : seul le
Client ID est nécessaire). Requiert une app Spotify développeur avec la « Web API » activée."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from .models import Track, normalize
from .platforms import _best, _query

API = "https://api.spotify.com/v1"
SCOPES = "playlist-modify-private playlist-modify-public"
_TRACK_URL = re.compile(r"open\.spotify\.com/(?:intl-[a-z-]+/)?track/([A-Za-z0-9]+)")


def client_id() -> str | None:
    return os.environ.get("SPOTIFY_CLIENT_ID")


def redirect_uri() -> str:
    return os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8000/callback")


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(cid: str, challenge: str, state: str) -> str:
    return "https://accounts.spotify.com/authorize?" + urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": redirect_uri(), "scope": SCOPES,
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state,
    })


async def exchange_code(client: httpx.AsyncClient, cid: str, code: str, verifier: str) -> str:
    r = await client.post("https://accounts.spotify.com/api/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
        "client_id": cid, "code_verifier": verifier,
    })
    r.raise_for_status()
    return r.json()["access_token"]


def _spotify_error(r: httpx.Response) -> str:
    if r.status_code == 403:
        return ("Spotify a refusé l'accès (403). L'app Spotify développeur doit avoir la « Web API » "
                "activée (réservé aux comptes Premium) et votre compte ajouté dans « User Management ».")
    return f"Erreur Spotify {r.status_code} : {r.text[:200]}"


async def find_uri(client: httpx.AsyncClient, headers: dict, t: Track) -> str | None:
    for p in t.availability:
        if p.platform == "spotify" and p.status == "available" and p.url:
            m = _TRACK_URL.search(p.url)
            if m:
                return f"spotify:track:{m.group(1)}"
    queries = ([f"isrc:{t.isrc}"] if t.isrc else []) + [f'track:"{t.title}" artist:"{t.artist}"', _query(t)]
    for q in queries:
        r = await client.get(f"{API}/search", headers=headers, params={"q": q, "type": "track", "limit": 10})
        if r.status_code != 200:
            raise RuntimeError(_spotify_error(r))
        items = r.json().get("tracks", {}).get("items", [])
        found = _best(t, [(", ".join(a["name"] for a in it["artists"]), it["name"], it["uri"]) for it in items])
        if found:
            return found[1]
    return None


async def _post_with_fallback(client: httpx.AsyncClient, headers: dict, urls: list[str], json: dict) -> httpx.Response:
    """Spotify a renommé certains points d'accès : on essaie le nouveau, puis l'ancien."""
    r = None
    for url in urls:
        r = await client.post(url, headers=headers, json=json)
        if r.status_code not in (404, 405):
            break
    if r.status_code >= 400:
        raise RuntimeError(_spotify_error(r))
    return r


@dataclass
class PlaylistResult:
    url: str
    added: list[Track] = field(default_factory=list)
    missing: list[Track] = field(default_factory=list)


async def create_playlist(
    client: httpx.AsyncClient, token: str, name: str, description: str, tracks: list[Track]
) -> PlaylistResult:
    headers = {"Authorization": f"Bearer {token}"}
    uris: list[str] = []
    added, missing = [], []
    for t in tracks:
        if not t.title or normalize(t.title) == "id":
            continue
        uri = await find_uri(client, headers, t)
        if uri is None:
            missing.append(t)
        elif uri not in uris:  # un morceau rejoué n'est ajouté qu'une fois
            uris.append(uri)
            added.append(t)

    me = await client.get(f"{API}/me", headers=headers)
    if me.status_code != 200:
        raise RuntimeError(_spotify_error(me))
    r = await _post_with_fallback(client, headers, [f"{API}/me/playlists", f"{API}/users/{me.json()['id']}/playlists"],
                                  {"name": name[:100], "description": description[:300], "public": False})
    playlist = r.json()
    for i in range(0, len(uris), 100):
        await _post_with_fallback(client, headers, [f"{API}/playlists/{playlist['id']}/items",
                                                    f"{API}/playlists/{playlist['id']}/tracks"],
                                  {"uris": uris[i:i + 100]})
    return PlaylistResult(url=playlist["external_urls"]["spotify"], added=added, missing=missing)
