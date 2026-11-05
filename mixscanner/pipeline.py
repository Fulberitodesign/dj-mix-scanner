"""Orchestration : téléchargement -> reconnaissance par fenêtres -> tracklist -> plateformes."""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .audio import SAMPLE_RATE, MixSource, default_cache_dir, extract_clip, load_source
from .models import Hit, Match, Track, fmt_time
from .platforms import PlatformChecker
from .recognizers import Recognizer, get_recognizer
from .tracklist import build_tracklist, merge_with_description, parse_description, use_match


@dataclass
class ScanOptions:
    step: float = 30.0          # un extrait analysé toutes les N secondes
    clip: float = 12.0          # durée de chaque extrait
    recognizer: str = "shazam"  # shazam | acrcloud | audd
    concurrency: int = 2        # requêtes simultanées (Shazam sature au-delà)
    cooldown: float = 15.0      # pause commune après un échec (s)
    min_hits: int = 1           # nb de détections minimum pour garder un morceau
    max_gap: float = 120.0
    window_timeout: float = 150.0  # délai max par extrait, relances comprises
    start: float = 0.0
    end: float | None = None
    check_platforms: bool = True
    use_description: bool = True
    cookies_from_browser: str | None = None  # chrome | safari | firefox… (si YouTube bloque)
    cache_dir: Path = field(default_factory=default_cache_dir)


@dataclass
class ScanResult:
    source: dict[str, Any]
    tracks: list[Track]
    windows: int
    recognized_windows: int
    description_entries: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "stats": {"windows": self.windows, "recognized_windows": self.recognized_windows,
                      "tracks": len(self.tracks), "description_entries": self.description_entries},
            "tracks": [t.to_dict() for t in self.tracks],
        }


MIN_CLIP_BYTES = 44 + SAMPLE_RATE * 2 * 2  # en-tête WAV + 2 s d'audio

Event = Callable[[str, dict[str, Any]], None]


def _noop(stage: str, data: dict[str, Any]) -> None:
    pass


def _hits_cache(opts: ScanOptions, src: MixSource) -> Path | None:
    key = src.cache_id or src.video_id
    if not key:
        return None
    return opts.cache_dir / f"hits-v3-{opts.recognizer}-{key}-{opts.step:g}-{opts.clip:g}.json"


def _legacy_hits_cache(opts: ScanOptions, src: MixSource) -> Path | None:
    """Nom utilisé par les versions précédentes : permet de reprendre un scan déjà commencé."""
    if not src.video_id:
        return None
    return opts.cache_dir / f"hits-v2-{opts.recognizer}-{src.video_id}-{opts.step:g}-{opts.clip:g}.json"


def _load_cache(*paths: Path | None) -> dict[float, Hit]:
    for path in paths:
        if not path or not path.exists():
            continue
        try:
            return {raw["offset"]: Hit(raw["offset"], Match(**raw["match"]) if raw["match"] else None)
                    for raw in json.loads(path.read_text())}
        except (ValueError, KeyError, TypeError):
            continue  # cache illisible (interruption pendant l'écriture…) : on l'ignore
    return {}


async def recognize_windows(
    src: MixSource, opts: ScanOptions, recognizer: Recognizer, on_event: Event = _noop
) -> list[Hit]:
    end = min(opts.end or src.duration, src.duration)
    offsets: list[float] = []
    t = opts.start
    while t + min(opts.clip, 5) <= end:
        offsets.append(t)
        t += opts.step

    cache = _hits_cache(opts, src)
    cached = _load_cache(cache, _legacy_hits_cache(opts, src))

    hits: list[Hit] = []
    failed: dict[float, str] = {}
    sem = asyncio.Semaphore(opts.concurrency)
    done = 0

    def save_cache() -> None:
        # Sauvegarde régulière : si le scan est interrompu, la relance reprend où il en était.
        # Les extraits en échec ne sont pas mis en cache : ils seront retentés.
        if not cache:
            return
        # On garde aussi les extraits d'analyses précédentes hors de la plage actuelle (--start/--end).
        keep = {o: h for o, h in cached.items() if o not in failed}
        keep.update({h.offset: h for h in hits if h.offset not in failed})
        data = json.dumps([{"offset": h.offset, "match": asdict(h.match) if h.match else None}
                           for h in sorted(keep.values(), key=lambda h: h.offset)])
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(data)
        tmp.replace(cache)  # écriture atomique : pas de cache à moitié écrit si on coupe

    loop = asyncio.get_running_loop()
    cooldown_until = 0.0

    async def attempt(offset: float) -> Hit:
        """Reconnaît un extrait. En cas d'échec (Shazam saturé), tous les extraits marquent
        une pause commune avant de continuer, pour laisser le service respirer."""
        nonlocal cooldown_until
        async with sem:
            while (wait := cooldown_until - loop.time()) > 0:
                await asyncio.sleep(wait)
            try:
                wav = await asyncio.wait_for(extract_clip(src.path, offset, opts.clip), 60)
                if len(wav) < MIN_CLIP_BYTES:  # fin du fichier : rien à reconnaître
                    hit = Hit(offset, None)
                else:
                    hit = Hit(offset, await asyncio.wait_for(recognizer.recognize(wav), opts.window_timeout))
                failed.pop(offset, None)
                return hit
            except Exception as e:  # une fenêtre en échec ne doit pas arrêter le scan
                if isinstance(e, asyncio.TimeoutError):
                    failed[offset] = "délai dépassé (le service de reconnaissance ne répond pas)"
                else:
                    failed[offset] = f"{type(e).__name__}: {e}"
                cooldown_until = max(cooldown_until, loop.time() + opts.cooldown)
                return Hit(offset, None)

    async def one(offset: float) -> None:
        nonlocal done
        hit = cached[offset] if offset in cached else await attempt(offset)
        if offset in failed:
            on_event("warning", {"message": f"Extrait à {fmt_time(offset)} : {failed[offset]}"})
        hits.append(hit)
        done += 1
        if done % 10 == 0:
            save_cache()
        on_event("recognize", {"done": done, "total": len(offsets), "hit": hit})

    await asyncio.gather(*(one(o) for o in offsets))

    # Deuxième passage sur les extraits en échec, après une pause, un par un.
    if failed and any(h.match for h in hits):
        on_event("retry", {"count": len(failed)})
        await asyncio.sleep(opts.cooldown)
        sem = asyncio.Semaphore(1)
        retried = {o: await attempt(o) for o in list(failed)}
        hits = [retried.get(h.offset, h) for h in hits]
        for h in retried.values():
            on_event("recognize", {"done": done, "total": len(offsets), "hit": h})

    hits.sort(key=lambda h: h.offset)

    # Échec total = problème de configuration : on l'affiche au lieu de « 0 morceau ».
    # Échec partiel : on garde les résultats ; les extraits en échec seront retentés au prochain scan.
    if failed and not any(h.match for h in hits):
        raise RuntimeError(
            f"La reconnaissance a échoué pour {len(failed)} extraits sur {len(hits)}. "
            f"Première erreur : {next(iter(failed.values()))}"
        )

    save_cache()
    return hits


async def pick_most_popular(tracks: list[Track], checker: PlatformChecker) -> None:
    """Quand Shazam hésite entre plusieurs versions détectées une fois chacune (original / reprise),
    on garde la plus écoutée sur Deezer : c'est presque toujours celle que le DJ a jouée."""
    for t in tracks:
        if not t.ambiguous:
            continue
        candidates = [None, *t.alternatives]  # None = détection actuellement retenue
        scores = []
        for c in candidates:
            artist, title = (t.artist, t.title) if c is None else (c.artist, c.title)
            try:
                scores.append(await checker.deezer_popularity(artist, title))
            except Exception:
                scores.append(-1)
        best = max(range(len(candidates)), key=lambda i: scores[i])
        if candidates[best] is not None and scores[best] > scores[0]:
            use_match(t, candidates[best])


async def _close(obj: Any) -> None:
    close = getattr(obj, "aclose", None)
    if close:
        try:
            await close()
        except Exception:
            pass


async def scan(
    url: str,
    opts: ScanOptions | None = None,
    on_event: Event = _noop,
    recognizer: Recognizer | None = None,
    checker: PlatformChecker | None = None,
) -> ScanResult:
    opts = opts or ScanOptions()

    on_event("download", {"progress": 0.0})
    src = await asyncio.to_thread(
        load_source, url, opts.cache_dir, lambda p: on_event("download", {"progress": p}),
        opts.cookies_from_browser,
    )
    source_info = {"title": src.title, "url": src.url or url, "duration": src.duration,
                   "uploader": src.uploader, "video_id": src.video_id}
    on_event("source", source_info)

    own_recognizer = recognizer is None
    recognizer = recognizer or get_recognizer(opts.recognizer)
    try:
        hits = await recognize_windows(src, opts, recognizer, on_event)
    finally:
        if own_recognizer:
            await _close(recognizer)
    tracks = build_tracklist(hits, opts.clip, src.duration, opts.max_gap, opts.min_hits)

    entries = parse_description(src.description, src.chapters) if opts.use_description else []
    # Seules les entrées situées dans la partie analysée comptent (--start / --end).
    end = min(opts.end or src.duration, src.duration)
    entries = [e for e in entries if opts.start - 5 <= e.start < end]
    if entries:
        tracks = merge_with_description(tracks, entries)
    on_event("tracklist", {"tracks": tracks})

    if opts.check_platforms and tracks:
        own_checker = checker is None
        checker = checker or PlatformChecker()
        sem = asyncio.Semaphore(3)
        done = 0
        await pick_most_popular(tracks, checker)
        on_event("tracklist", {"tracks": tracks})

        async def check(t: Track) -> None:
            nonlocal done
            async with sem:
                t.availability = await checker.check(t)
            done += 1
            on_event("platforms", {"done": done, "total": len(tracks), "track": t})

        try:
            await asyncio.gather(*(check(t) for t in tracks))
        finally:
            if own_checker:
                await checker.aclose()

    return ScanResult(
        source=source_info, tracks=tracks, windows=len(hits),
        recognized_windows=sum(1 for h in hits if h.match), description_entries=len(entries),
    )
