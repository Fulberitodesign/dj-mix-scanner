"""Orchestration : téléchargement -> reconnaissance par fenêtres -> tracklist -> plateformes."""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .audio import MixSource, default_cache_dir, extract_clip, load_source
from .models import Hit, Match, Track
from .platforms import PlatformChecker
from .recognizers import Recognizer, get_recognizer
from .tracklist import build_tracklist, merge_with_description, parse_description


@dataclass
class ScanOptions:
    step: float = 30.0          # un extrait analysé toutes les N secondes
    clip: float = 12.0          # durée de chaque extrait
    recognizer: str = "shazam"  # shazam | acrcloud | audd
    concurrency: int = 4
    min_hits: int = 1           # nb de détections minimum pour garder un morceau
    max_gap: float = 120.0
    start: float = 0.0
    end: float | None = None
    check_platforms: bool = True
    use_description: bool = True
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


Event = Callable[[str, dict[str, Any]], None]


def _noop(stage: str, data: dict[str, Any]) -> None:
    pass


def _hits_cache(opts: ScanOptions, src: MixSource) -> Path | None:
    if not src.video_id:
        return None
    return opts.cache_dir / f"hits-{opts.recognizer}-{src.video_id}-{opts.step:g}-{opts.clip:g}.json"


async def recognize_windows(
    src: MixSource, opts: ScanOptions, recognizer: Recognizer, on_event: Event = _noop
) -> list[Hit]:
    end = min(opts.end or src.duration, src.duration)
    offsets: list[float] = []
    t = opts.start
    while t + min(opts.clip, 5) <= end:
        offsets.append(t)
        t += opts.step

    cached: dict[float, Hit] = {}
    cache = _hits_cache(opts, src)
    if cache and cache.exists():
        for raw in json.loads(cache.read_text()):
            cached[raw["offset"]] = Hit(raw["offset"], Match(**raw["match"]) if raw["match"] else None)

    hits: list[Hit] = []
    sem = asyncio.Semaphore(opts.concurrency)
    done = 0

    async def one(offset: float) -> None:
        nonlocal done
        if offset in cached:
            hit = cached[offset]
        else:
            async with sem:
                try:
                    wav = await extract_clip(src.path, offset, opts.clip)
                    hit = Hit(offset, await recognizer.recognize(wav))
                except Exception as e:  # une fenêtre en échec ne doit pas arrêter le scan
                    on_event("warning", {"message": f"Fenêtre {offset:.0f}s : {e}"})
                    hit = Hit(offset, None)
        hits.append(hit)
        done += 1
        on_event("recognize", {"done": done, "total": len(offsets), "hit": hit})

    await asyncio.gather(*(one(o) for o in offsets))
    hits.sort(key=lambda h: h.offset)

    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps([{"offset": h.offset, "match": asdict(h.match) if h.match else None}
                                     for h in hits]))
    return hits


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
        load_source, url, opts.cache_dir, lambda p: on_event("download", {"progress": p})
    )
    source_info = {"title": src.title, "url": src.url or url, "duration": src.duration,
                   "uploader": src.uploader, "video_id": src.video_id}
    on_event("source", source_info)

    recognizer = recognizer or get_recognizer(opts.recognizer)

    hits = await recognize_windows(src, opts, recognizer, on_event)
    tracks = build_tracklist(hits, opts.clip, src.duration, opts.max_gap, opts.min_hits)

    entries = parse_description(src.description, src.chapters) if opts.use_description else []
    if entries:
        tracks = merge_with_description(tracks, entries)
    on_event("tracklist", {"tracks": tracks})

    if opts.check_platforms and tracks:
        checker = checker or PlatformChecker()
        sem = asyncio.Semaphore(3)
        done = 0

        async def check(t: Track) -> None:
            nonlocal done
            async with sem:
                t.availability = await checker.check(t)
            done += 1
            on_event("platforms", {"done": done, "total": len(tracks), "track": t})

        await asyncio.gather(*(check(t) for t in tracks))

    return ScanResult(
        source=source_info, tracks=tracks, windows=len(hits),
        recognized_windows=sum(1 for h in hits if h.match), description_entries=len(entries),
    )
