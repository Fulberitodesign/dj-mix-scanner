"""Ligne de commande : python -m mixscanner "https://www.youtube.com/watch?v=..." """
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .export import EXPORTERS, PLATFORM_NAMES, _platform_cell, _version
from .models import fmt_time
from .pipeline import ScanOptions, scan


def _progress(stage: str, data: dict) -> None:
    if stage == "download":
        print(f"\r⬇️  Téléchargement… {data['progress']*100:5.1f} %", end="", file=sys.stderr, flush=True)
    elif stage == "source":
        print(f"\n🎧 {data['title']} ({fmt_time(data['duration'])})", file=sys.stderr)
    elif stage == "recognize":
        hit = data["hit"]
        found = f"{hit.match.artist} - {hit.match.title}" if hit.match else "—"
        print(f"\r🔍 {data['done']}/{data['total']}  [{fmt_time(hit.offset)}] {found[:70]:<70}",
              end="", file=sys.stderr, flush=True)
    elif stage == "tracklist":
        print(f"\n📋 {len(data['tracks'])} morceaux détectés", file=sys.stderr)
    elif stage == "platforms":
        print(f"\r🌐 Plateformes {data['done']}/{data['total']}", end="", file=sys.stderr, flush=True)
    elif stage == "warning":
        print(f"\n⚠️  {data['message']}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mixscanner", description="Identifie les morceaux d'un mix DJ, dans l'ordre.")
    ap.add_argument("url", help="Lien YouTube / SoundCloud / Mixcloud… ou fichier audio local")
    ap.add_argument("--step", type=float, default=30, help="Analyser un extrait toutes les N secondes (défaut 30)")
    ap.add_argument("--clip", type=float, default=12, help="Durée de chaque extrait en secondes (défaut 12)")
    ap.add_argument("--recognizer", default="shazam", choices=["shazam", "acrcloud", "audd"])
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--min-hits", type=int, default=1, help="Détections minimum pour garder un morceau")
    ap.add_argument("--start", type=float, default=0, help="Commencer à N secondes")
    ap.add_argument("--end", type=float, default=None, help="S'arrêter à N secondes")
    ap.add_argument("--no-platforms", action="store_true", help="Ne pas vérifier Spotify/SoundCloud/…")
    ap.add_argument("--no-description", action="store_true", help="Ignorer la tracklist de la description")
    ap.add_argument("-o", "--output", type=Path, help="Fichier de sortie (.txt .csv .json .md)")
    args = ap.parse_args(argv)

    opts = ScanOptions(step=args.step, clip=args.clip, recognizer=args.recognizer, concurrency=args.concurrency,
                       min_hits=args.min_hits, start=args.start, end=args.end,
                       check_platforms=not args.no_platforms, use_description=not args.no_description)
    res = asyncio.run(scan(args.url, opts, _progress))
    print("\n", file=sys.stderr)

    for t in res.tracks:
        name = f"{t.artist} - {t.title}" if t.artist else t.title
        src = "" if t.source == "audio" else f"  ({t.source})"
        print(f"{t.index:>3}. {fmt_time(t.start):>8}  {name}  [{_version(t)}]{src}")
        for p in t.availability:
            if p.status == "available":
                print(f"         {_platform_cell(p)} {PLATFORM_NAMES[p.platform]}: {p.url}")
        missing = [PLATFORM_NAMES[p.platform] for p in t.availability if p.status == "not_found"]
        if missing:
            print(f"         ❌ introuvable sur : {', '.join(missing)}")

    if args.output:
        fmt = args.output.suffix.lstrip(".").lower()
        if fmt not in EXPORTERS:
            ap.error(f"Format non supporté : .{fmt}")
        args.output.write_text(EXPORTERS[fmt](res), encoding="utf-8")
        print(f"\n💾 Enregistré : {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
