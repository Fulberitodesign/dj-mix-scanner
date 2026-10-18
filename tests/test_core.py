import asyncio
import io
import json
import subprocess
import wave
from pathlib import Path

import httpx
import pytest

from mixscanner.audio import ffmpeg_exe
from mixscanner.classify import classify, same_version
from mixscanner.export import EXPORTERS
from mixscanner.models import Hit, Match, Track
from mixscanner.pipeline import ScanOptions, scan
from mixscanner.platforms import PlatformChecker
from mixscanner.recognizers import ACRCloudRecognizer, ShazamRecognizer
from mixscanner.tracklist import build_tracklist, merge_with_description, parse_description


# --- classification des versions ---------------------------------------------------

@pytest.mark.parametrize("title,artist,kind,remixer", [
    ("Losing It", "FISHER", "original", None),
    ("Losing It (Original Mix)", "FISHER", "original", None),
    ("Innerbloom (What So Not Remix)", "RÜFÜS DU SOL", "remix", "What So Not"),
    ("Titanium [David Guetta VIP]", "David Guetta", "vip", "David Guetta"),
    ("One More Time (Mashup)", "Daft Punk", "mashup", None),
    ("Levels", "Avicii vs Daft Punk", "mashup", None),
    ("Strobe - Dimension Bootleg", "deadmau5", "bootleg", "Dimension"),
    ("Blue Monday (Joe Goddard Re-Edit)", "New Order", "edit", "Joe Goddard"),
    ("Wake Me Up (Radio Edit)", "Avicii", "original", None),
    ("Opus (Extended Mix)", "Eric Prydz", "extended", None),
])
def test_classify(title, artist, kind, remixer):
    v = classify(title, artist)
    assert v.kind == kind
    assert v.remixer == remixer


def test_same_version():
    assert same_version("Opus", "Opus (Original Mix)")
    assert same_version("Opus (Extended Mix)", "Opus")
    assert not same_version("Innerbloom", "Innerbloom (What So Not Remix)")
    assert not same_version("Innerbloom (Lane 8 Remix)", "Innerbloom (What So Not Remix)")


# --- construction de la tracklist ----------------------------------------------------

def m(title, artist="X"):
    return Match(title=title, artist=artist, provider="test")


def test_build_tracklist_groups_and_tolerates_gaps_and_glitches():
    hits = [
        Hit(0, m("A")), Hit(30, m("A")), Hit(60, None), Hit(90, m("A")),
        Hit(120, m("B")), Hit(150, m("Z")), Hit(180, m("B")), Hit(210, m("B")),
        Hit(240, None), Hit(270, m("C")),
    ]
    tracks = build_tracklist(hits, clip_length=12, duration=300)
    assert [t.title for t in tracks] == ["A", "B", "C"]
    assert [t.start for t in tracks] == [0, 120, 270]
    assert tracks[0].hits == 3 and tracks[0].confidence == "high"
    assert tracks[1].hits == 3  # Z en sandwich absorbé
    assert tracks[0].end == 102 and tracks[2].end == 282


def test_same_track_played_twice_far_apart_is_listed_twice():
    hits = [Hit(0, m("A")), Hit(30, m("B")), Hit(60, m("B")), Hit(400, m("A"))]
    assert [t.title for t in build_tracklist(hits, 12, max_gap=120)] == ["A", "B", "A"]


def test_min_hits_filters_single_detections():
    hits = [Hit(0, m("A")), Hit(30, m("A")), Hit(60, m("Noise")), Hit(90, m("C")), Hit(120, m("C"))]
    assert [t.title for t in build_tracklist(hits, 12, min_hits=2)] == ["A", "C"]


DESC = """Merci d'avoir écouté !
Tracklist :
00:00 FISHER - Losing It
[03:15] RÜFÜS DU SOL – Innerbloom (What So Not Remix)
1. 7:40 ID - ID
Daft Punk - One More Time (1:02:10)
Suivez-moi sur instagram
"""


def test_parse_description():
    e = parse_description(DESC)
    assert [(x.start, x.artist, x.title) for x in e] == [
        (0, "FISHER", "Losing It"),
        (195, "RÜFÜS DU SOL", "Innerbloom (What So Not Remix)"),
        (460, "ID", "ID"),
        (3730, "Daft Punk", "One More Time"),
    ]


def test_parse_chapters_preferred():
    ch = [{"start_time": 0, "title": "Intro"}, {"start_time": 60, "title": "Bicep - Glue"}]
    e = parse_description(DESC, ch)
    assert [(x.start, x.artist, x.title) for x in e] == [(0, "", "Intro"), (60, "Bicep", "Glue")]


def test_merge_with_description():
    tracks = build_tracklist([Hit(10, m("Losing It", "FISHER")), Hit(40, m("Losing It", "FISHER"))], 12)
    merged = merge_with_description(tracks, parse_description(DESC))
    assert [(t.title, t.source) for t in merged] == [
        ("Losing It", "audio+description"),
        ("Innerbloom (What So Not Remix)", "description"),
        ("One More Time", "description"),
    ]
    assert merged[1].version.kind == "remix"
    assert [t.index for t in merged] == [1, 2, 3]


# --- parsing des réponses des services ------------------------------------------------

def test_parse_shazam():
    out = {"track": {
        "key": "123", "title": "Losing It", "subtitle": "FISHER", "isrc": "AUUM71800540",
        "url": "https://www.shazam.com/track/123", "images": {"coverart": "https://img/c.jpg"},
        "hub": {"options": [{"actions": [{"uri": "https://music.apple.com/fr/album/x?i=1"}]}]},
        "sections": [{"metadata": [{"title": "Album", "text": "Losing It"}, {"title": "Label", "text": "Catch & Release"},
                                   {"title": "Released", "text": "2018"}]}],
    }}
    r = ShazamRecognizer.parse(out)
    assert (r.title, r.artist, r.isrc, r.label, r.year) == ("Losing It", "FISHER", "AUUM71800540", "Catch & Release", "2018")
    assert r.links["apple_music"].startswith("https://music.apple.com")
    assert ShazamRecognizer.parse({"matches": []}) is None


def test_parse_acrcloud():
    out = {"status": {"code": 0}, "metadata": {"music": [{
        "title": "Glue", "artists": [{"name": "Bicep"}], "score": 92, "acrid": "abc",
        "external_ids": {"isrc": "GBCFB1700283"}, "release_date": "2017-09-01",
        "external_metadata": {"spotify": {"track": {"id": "sp1"}}, "deezer": {"track": {"id": 42}}},
    }]}}
    r = ACRCloudRecognizer.parse(out)
    assert (r.title, r.artist, r.year, r.isrc) == ("Glue", "Bicep", "2017", "GBCFB1700283")
    assert r.links == {"spotify": "https://open.spotify.com/track/sp1", "deezer": "https://www.deezer.com/track/42"}
    assert ACRCloudRecognizer.parse({"status": {"code": 1001}}) is None


# --- vérification des plateformes (API simulées) -------------------------------------

def mock_client():
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "deezer.com/search" in url:
            return httpx.Response(200, json={"data": [
                {"title": "Innerbloom", "link": "https://www.deezer.com/track/1", "artist": {"name": "RÜFÜS DU SOL"}},
            ]})
        if "itunes.apple.com" in url:
            return httpx.Response(200, json={"results": [
                {"artistName": "RÜFÜS DU SOL", "trackName": "Innerbloom (What So Not Remix)",
                 "trackViewUrl": "https://music.apple.com/x"},
            ]})
        if "accounts.spotify.com" in url:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if "api.spotify.com" in url:
            assert req.headers["Authorization"] == "Bearer tok"
            return httpx.Response(200, json={"tracks": {"items": [
                {"name": "Something Else", "artists": [{"name": "Nobody"}], "external_urls": {"spotify": "https://sp/x"}},
            ]}})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_platform_checker(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")
    monkeypatch.delenv("SOUNDCLOUD_CLIENT_ID", raising=False)
    t = Track(index=1, start=0, end=60, title="Innerbloom (What So Not Remix)", artist="RÜFÜS DU SOL",
              version=classify("Innerbloom (What So Not Remix)"))
    res = {p.platform: p for p in asyncio.run(PlatformChecker(mock_client(), youtube=False).check(t))}
    assert res["apple_music"].status == "available" and res["apple_music"].same_version is True
    assert res["deezer"].status == "available" and res["deezer"].same_version is False  # seulement l'original
    assert res["spotify"].status == "not_found"
    assert res["soundcloud"].status == "unverified" and "soundcloud.com/search" in res["soundcloud"].search_url
    assert res["youtube"].status == "unverified"
    assert set(res) == {"spotify", "soundcloud", "deezer", "apple_music", "youtube", "beatport", "google"}


# --- bout en bout : vrai découpage ffmpeg + faux "Shazam" -----------------------------

SECTIONS = [(0, 90, 440, "Track A"), (90, 200, 880, "Track B (Fisher Remix)"), (200, 260, 660, "Track C")]


class FrequencyRecognizer:
    """Reconnaît la fréquence dominante de l'extrait : prouve que les offsets ffmpeg sont justes."""
    name = "fake"

    async def recognize(self, wav: bytes):
        with wave.open(io.BytesIO(wav)) as w:
            rate, n = w.getframerate(), w.getnframes()
            data = w.readframes(n)
        samples = [int.from_bytes(data[i:i + 2], "little", signed=True) for i in range(0, len(data), 2)]
        crossings = sum(1 for a, b in zip(samples, samples[1:]) if a < 0 <= b)
        freq = crossings / (len(samples) / rate)
        for *_, f, title in SECTIONS:
            if abs(freq - f) < 15:
                return Match(title=title, artist="DJ Test", provider="fake")
        return None


@pytest.fixture(scope="module")
def mix_file(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("mix") / "mix.m4a"
    inputs, filters = [], []
    for i, (start, end, f, _) in enumerate(SECTIONS):
        inputs += ["-f", "lavfi", "-i", f"sine=frequency={f}:duration={end - start}"]
        filters.append(f"[{i}:a]")
    subprocess.run([ffmpeg_exe(), "-v", "error", "-y", *inputs, "-filter_complex",
                    "".join(filters) + f"concat=n={len(SECTIONS)}:v=0:a=1", str(path)], check=True)
    return path


def test_end_to_end(mix_file, tmp_path):
    events = []
    opts = ScanOptions(step=20, clip=10, check_platforms=False, cache_dir=tmp_path)
    res = asyncio.run(scan(str(mix_file), opts, lambda s, d: events.append(s), recognizer=FrequencyRecognizer()))
    assert [(t.title, t.start) for t in res.tracks] == [("Track A", 0), ("Track B (Fisher Remix)", 100), ("Track C", 200)]
    assert res.tracks[1].version.kind == "remix" and res.tracks[1].version.remixer == "Fisher"
    assert "recognize" in events and "tracklist" in events
    for fmt, fn in EXPORTERS.items():
        assert "Track C" in fn(res)
    assert json.loads(EXPORTERS["json"](res))["stats"]["tracks"] == 3


class BrokenRecognizer:
    name = "broken"

    async def recognize(self, wav: bytes):
        raise ConnectionError("SSL: CERTIFICATE_VERIFY_FAILED")


def test_all_windows_failing_is_reported_not_silent(mix_file, tmp_path):
    warnings = []
    opts = ScanOptions(step=60, clip=10, check_platforms=False, cache_dir=tmp_path, cooldown=0.1)
    with pytest.raises(RuntimeError, match="CERTIFICATE_VERIFY_FAILED"):
        asyncio.run(scan(str(mix_file), opts, lambda s, d: s == "warning" and warnings.append(d),
                         recognizer=BrokenRecognizer()))
    assert len(warnings) == 5


def test_certifi_bundle_is_configured():
    import os
    import mixscanner  # noqa: F401
    assert os.environ.get("SSL_CERT_FILE")


def test_bundled_ffmpeg_is_exposed_on_path(monkeypatch, tmp_path, mix_file):
    """shazamio_core appelle « ffmpeg » directement : il doit être trouvable même sans ffmpeg système."""
    import shutil
    from mixscanner import audio
    from mixscanner.audio import extract_clip

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("MIXSCANNER_CACHE", str(tmp_path / "cache"))
    audio.ffmpeg_exe.cache_clear()
    try:
        exe = audio.ffmpeg_exe()
        assert shutil.which("ffmpeg") == exe
        from shazamio import Shazam

        async def signature():
            wav = await extract_clip(mix_file, 5, 10)
            return await Shazam().core_recognizer.recognize_bytes(value=wav, options=None)

        assert asyncio.run(signature()).signature.samples > 0
    finally:
        audio.ffmpeg_exe.cache_clear()


# --- blocages et playlist -------------------------------------------------------------

class HangingRecognizer:
    """Répond normalement sauf sur certains extraits où il ne répond jamais (comme Shazam saturé)."""
    name = "hang"

    def __init__(self):
        self.calls = 0

    async def recognize(self, wav: bytes):
        self.calls += 1
        if self.calls % 2 == 0:
            await asyncio.sleep(3600)
        return await FrequencyRecognizer().recognize(wav)


def test_hanging_requests_do_not_freeze_the_scan(mix_file, tmp_path):
    warnings = []
    opts = ScanOptions(step=20, clip=10, check_platforms=False, cache_dir=tmp_path, window_timeout=0.5, cooldown=0.1)
    res = asyncio.run(scan(str(mix_file), opts, lambda s, d: s == "warning" and warnings.append(d),
                           recognizer=HangingRecognizer()))
    assert res.recognized_windows > 0 and warnings  # résultats partiels gardés, échecs signalés
    assert [t.title for t in res.tracks] == ["Track A", "Track B (Fisher Remix)", "Track C"]


def _result_with(tracks):
    from mixscanner.pipeline import ScanResult
    return ScanResult(source={"title": "Mix", "url": "u"}, tracks=tracks, windows=0, recognized_windows=0,
                      description_entries=0)


def _t(title, artist, **kw):
    return Track(index=0, start=0, end=0, title=title, artist=artist, version=classify(title), **kw)


def test_playlist_export_dedupes_and_skips_ids():
    res = _result_with([_t("Glue", "Bicep"), _t("ID", "ID"), _t("Opus", "Eric Prydz"), _t("Glue", "Bicep")])
    assert EXPORTERS["playlist"](res) == "Bicep - Glue\nEric Prydz - Opus\n"


def test_create_spotify_playlist_with_endpoint_fallback():
    from mixscanner.models import PlatformResult
    from mixscanner.spotify_playlist import create_playlist

    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        path = req.url.path
        if path == "/v1/search":
            return httpx.Response(200, json={"tracks": {"items": [
                {"name": "Opus", "artists": [{"name": "Eric Prydz"}], "uri": "spotify:track:opus"}]}})
        if path == "/v1/me" and req.method == "GET":
            return httpx.Response(200, json={"id": "dj"})
        if path == "/v1/me/playlists":
            return httpx.Response(404)  # ancien point d'accès seulement
        if path == "/v1/users/dj/playlists":
            assert json.loads(req.content)["name"] == "Mon mix"
            return httpx.Response(201, json={"id": "pl1", "external_urls": {"spotify": "https://open.spotify.com/playlist/pl1"}})
        if path == "/v1/playlists/pl1/items":
            assert json.loads(req.content)["uris"] == ["spotify:track:glue", "spotify:track:opus"]
            return httpx.Response(201, json={"snapshot_id": "x"})
        return httpx.Response(404)

    tracks = [
        _t("Glue", "Bicep", availability=[PlatformResult("spotify", "available", url="https://open.spotify.com/track/glue")]),
        _t("Opus", "Eric Prydz"),
        _t("Glue", "Bicep", availability=[PlatformResult("spotify", "available", url="https://open.spotify.com/intl-fr/track/glue")]),
        _t("ID", "ID"),
    ]

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await create_playlist(c, "tok", "Mon mix", "desc", tracks)

    pl = asyncio.run(run())
    assert pl.url.endswith("/pl1")
    assert [t.title for t in pl.added] == ["Glue", "Opus"] and pl.missing == []
    assert ("POST", "/v1/me/playlists") in calls and ("POST", "/v1/users/dj/playlists") in calls


def test_spotify_403_gives_clear_message():
    from mixscanner.spotify_playlist import create_playlist

    async def run():
        transport = httpx.MockTransport(lambda req: httpx.Response(403, text="forbidden"))
        async with httpx.AsyncClient(transport=transport) as c:
            await create_playlist(c, "tok", "Mix", "", [_t("Glue", "Bicep")])

    with pytest.raises(RuntimeError, match="Web API"):
        asyncio.run(run())


class FlakyRecognizer:
    """Échoue au premier essai sur chaque extrait, réussit au second."""
    name = "flaky"

    def __init__(self):
        self.seen = set()

    async def recognize(self, wav: bytes):
        key = hash(wav)
        if key not in self.seen:
            self.seen.add(key)
            if len(self.seen) % 3 == 0:
                raise TimeoutError()
        return await FrequencyRecognizer().recognize(wav)


def test_failed_windows_are_retried_in_a_second_pass(mix_file, tmp_path):
    events = []
    opts = ScanOptions(step=20, clip=10, check_platforms=False, cache_dir=tmp_path, cooldown=0.05)
    res = asyncio.run(scan(str(mix_file), opts, lambda s, d: events.append(s), recognizer=FlakyRecognizer()))
    assert "retry" in events
    assert res.recognized_windows == res.windows  # tout récupéré au second passage


def test_odesli_resolves_spotify_links_in_mix_order():
    from mixscanner.models import PlatformResult
    from mixscanner.odesli import resolve_spotify_links

    asked = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host != "api.song.link":  # recherches Deezer / Apple : rien trouvé
            return httpx.Response(200, json={"data": [], "results": []})
        src = req.url.params["url"]
        asked.append(src)
        if src == "https://www.deezer.com/track/1":
            return httpx.Response(200, json={"linksByPlatform": {"spotify": {"url": "https://open.spotify.com/track/A"}}})
        if src == "https://music.apple.com/x":
            return httpx.Response(200, json={"linksByPlatform": {"spotify": {"url": "https://open.spotify.com/track/B"}}})
        return httpx.Response(200, json={"linksByPlatform": {}})

    tracks = [
        _t("Glue", "Bicep", availability=[PlatformResult("deezer", "available", url="https://www.deezer.com/track/1")]),
        _t("Opus", "Eric Prydz", links={"apple_music": "https://music.apple.com/x"}),
        _t("ID", "ID"),
        _t("Rare Dub", "Nobody", availability=[PlatformResult("youtube", "available", url="https://youtu.be/z")]),
        _t("No Source", "Nobody"),
        _t("Glue", "Bicep", availability=[PlatformResult("spotify", "available", url="https://open.spotify.com/track/A")]),
    ]
    progress = []

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await resolve_spotify_links(tracks, c, lambda d, t: progress.append((d, t)), delay=0)

    links, missing, diag = asyncio.run(run())
    assert links == ["https://open.spotify.com/track/A", "https://open.spotify.com/track/B"]
    assert [t.title for t in missing] == ["Rare Dub", "No Source"]
    assert asked == ["https://www.deezer.com/track/1", "https://music.apple.com/x", "https://youtu.be/z"]
    assert diag["not_on_spotify"] == 1 and diag["no_source"] == 1
    assert tracks[1].availability[0].platform == "spotify" and tracks[1].availability[0].status == "available"
    assert progress[-1] == (5, 5)



def test_odesli_searches_deezer_when_track_has_no_link():
    from mixscanner.odesli import resolve_spotify_links

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.deezer.com":
            return httpx.Response(200, json={"data": [
                {"title": "Umbrella", "link": "https://www.deezer.com/track/9", "artist": {"name": "Rihanna"}}]})
        if req.url.host == "api.song.link":
            assert req.url.params["url"] == "https://www.deezer.com/track/9"
            return httpx.Response(200, json={"linksByPlatform": {"spotify": {"url": "https://open.spotify.com/track/U"}}})
        return httpx.Response(200, json={"results": []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await resolve_spotify_links([_t("Umbrella (feat. JAY-Z)", "Rihanna")], c, delay=0)

    links, missing, diag = asyncio.run(run())
    assert links == ["https://open.spotify.com/track/U"] and missing == []


def test_odesli_errors_are_reported():
    from mixscanner.models import PlatformResult
    from mixscanner.odesli import resolve_spotify_links

    async def run():
        transport = httpx.MockTransport(lambda req: httpx.Response(500, text="boom"))
        async with httpx.AsyncClient(transport=transport) as c:
            t = _t("Glue", "Bicep", availability=[PlatformResult("deezer", "available", url="https://www.deezer.com/track/1")])
            return await resolve_spotify_links([t], c, delay=0)

    links, missing, diag = asyncio.run(run())
    assert links == [] and diag["errors"] == 1 and "500" in diag["first_error"]
