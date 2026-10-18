"""Services de reconnaissance audio : Shazam (gratuit, sans clé), ACRCloud et AudD (avec clé).

ACRCloud est le plus performant sur les mixes DJ (tolère pitch/tempo modifiés et voix par-dessus).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import time
from typing import Protocol

import httpx

from .models import Match


class Recognizer(Protocol):
    name: str

    async def recognize(self, wav: bytes) -> Match | None: ...


async def _with_retries(coro_factory, attempts: int = 4, base_delay: float = 2.0, timeout: float = 25.0):
    """Relance en cas d'échec ; chaque tentative est limitée à `timeout` secondes
    (Shazam peut laisser une requête pendante indéfiniment quand il limite le débit)."""
    for i in range(attempts):
        try:
            return await asyncio.wait_for(coro_factory(), timeout)
        except Exception:
            if i == attempts - 1:
                raise
            await asyncio.sleep(base_delay * 2**i)


class ShazamRecognizer:
    name = "shazam"

    def __init__(self) -> None:
        from shazamio import Shazam

        from .audio import ffmpeg_exe

        ffmpeg_exe()  # shazamio_core a besoin de « ffmpeg » dans le PATH
        self._shazam = Shazam()

    async def recognize(self, wav: bytes) -> Match | None:
        out = await _with_retries(lambda: self._shazam.recognize(wav))
        return self.parse(out)

    @staticmethod
    def parse(out: dict) -> Match | None:
        t = (out or {}).get("track")
        if not t:
            return None
        meta = {}
        for section in t.get("sections", []):
            for item in section.get("metadata", []) or []:
                meta[item.get("title", "").lower()] = item.get("text")
        links: dict[str, str] = {}
        if t.get("url"):
            links["shazam"] = t["url"]
        for opt in (t.get("hub") or {}).get("options", []):
            for action in opt.get("actions", []):
                uri = action.get("uri", "")
                if uri.startswith("https://music.apple.com") or "itunes.apple.com" in uri:
                    links.setdefault("apple_music", uri)
        return Match(
            title=t.get("title", ""),
            artist=t.get("subtitle", ""),
            provider="shazam",
            provider_id=str(t.get("key") or "") or None,
            isrc=t.get("isrc"),
            album=meta.get("album"),
            label=meta.get("label"),
            year=meta.get("released"),
            cover=(t.get("images") or {}).get("coverart"),
            links=links,
        )


class ACRCloudRecognizer:
    """https://www.acrcloud.com — variables ACRCLOUD_HOST, ACRCLOUD_ACCESS_KEY, ACRCLOUD_ACCESS_SECRET."""

    name = "acrcloud"

    def __init__(self, host: str, access_key: str, access_secret: str, min_score: int = 70) -> None:
        self.host, self.key, self.secret, self.min_score = host, access_key, access_secret, min_score
        self._client = httpx.AsyncClient(timeout=30)

    @classmethod
    def from_env(cls) -> "ACRCloudRecognizer":
        try:
            return cls(os.environ["ACRCLOUD_HOST"], os.environ["ACRCLOUD_ACCESS_KEY"],
                       os.environ["ACRCLOUD_ACCESS_SECRET"])
        except KeyError as e:
            raise RuntimeError(f"Variable d'environnement manquante pour ACRCloud : {e}") from None

    def _sign(self, timestamp: str) -> str:
        to_sign = "\n".join(["POST", "/v1/identify", self.key, "audio", "1", timestamp])
        digest = hmac.new(self.secret.encode(), to_sign.encode(), hashlib.sha1).digest()
        return base64.b64encode(digest).decode()

    async def recognize(self, wav: bytes) -> Match | None:
        ts = str(int(time.time()))
        data = {
            "access_key": self.key, "sample_bytes": str(len(wav)), "timestamp": ts,
            "signature": self._sign(ts), "data_type": "audio", "signature_version": "1",
        }

        async def call():
            r = await self._client.post(f"https://{self.host}/v1/identify", data=data,
                                        files={"sample": ("clip.wav", wav, "audio/wav")})
            r.raise_for_status()
            return r.json()

        return self.parse(await _with_retries(call), self.min_score)

    @staticmethod
    def parse(out: dict, min_score: int = 70) -> Match | None:
        if (out.get("status") or {}).get("code") != 0:
            return None
        music = (out.get("metadata") or {}).get("music") or []
        if not music:
            return None
        m = max(music, key=lambda x: x.get("score", 0))
        if m.get("score", 100) < min_score:
            return None
        ext = m.get("external_metadata") or {}
        links: dict[str, str] = {}
        sp = ((ext.get("spotify") or {}).get("track") or {}).get("id")
        if sp:
            links["spotify"] = f"https://open.spotify.com/track/{sp}"
        dz = ((ext.get("deezer") or {}).get("track") or {}).get("id")
        if dz:
            links["deezer"] = f"https://www.deezer.com/track/{dz}"
        yt = (ext.get("youtube") or {}).get("vid")
        if yt:
            links["youtube"] = f"https://www.youtube.com/watch?v={yt}"
        return Match(
            title=m.get("title", ""),
            artist=", ".join(a.get("name", "") for a in m.get("artists", [])),
            provider="acrcloud",
            provider_id=m.get("acrid"),
            isrc=(m.get("external_ids") or {}).get("isrc"),
            album=(m.get("album") or {}).get("name"),
            label=m.get("label"),
            year=(m.get("release_date") or "")[:4] or None,
            score=m.get("score"),
            links=links,
        )


class AuddRecognizer:
    """https://audd.io — variable AUDD_API_TOKEN."""

    name = "audd"

    def __init__(self, token: str) -> None:
        self.token = token
        self._client = httpx.AsyncClient(timeout=30)

    @classmethod
    def from_env(cls) -> "AuddRecognizer":
        token = os.environ.get("AUDD_API_TOKEN")
        if not token:
            raise RuntimeError("Variable d'environnement AUDD_API_TOKEN manquante.")
        return cls(token)

    async def recognize(self, wav: bytes) -> Match | None:
        async def call():
            r = await self._client.post(
                "https://api.audd.io/",
                data={"api_token": self.token, "return": "spotify,apple_music,deezer"},
                files={"file": ("clip.wav", wav, "audio/wav")},
            )
            r.raise_for_status()
            return r.json()

        return self.parse(await _with_retries(call))

    @staticmethod
    def parse(out: dict) -> Match | None:
        res = out.get("result") if out.get("status") == "success" else None
        if not res:
            return None
        links: dict[str, str] = {}
        if res.get("song_link"):
            links["songlink"] = res["song_link"]
        sp = ((res.get("spotify") or {}).get("external_urls") or {}).get("spotify")
        if sp:
            links["spotify"] = sp
        if (res.get("apple_music") or {}).get("url"):
            links["apple_music"] = res["apple_music"]["url"]
        if (res.get("deezer") or {}).get("link"):
            links["deezer"] = res["deezer"]["link"]
        return Match(
            title=res.get("title", ""), artist=res.get("artist", ""), provider="audd",
            album=res.get("album"), label=res.get("label"),
            year=(res.get("release_date") or "")[:4] or None, links=links,
        )


def get_recognizer(name: str) -> Recognizer:
    name = name.lower()
    if name == "shazam":
        return ShazamRecognizer()
    if name == "acrcloud":
        return ACRCloudRecognizer.from_env()
    if name == "audd":
        return AuddRecognizer.from_env()
    raise ValueError(f"Service de reconnaissance inconnu : {name} (shazam | acrcloud | audd)")
