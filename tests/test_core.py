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
    ("EoO (Afro House Remix)", "CYCLOPE", "remix", None),
    ("NEL (JPTheDJ Tech House Remix)", "Fuerza Regida", "remix", "JPTheDJ"),
    ("Who's That Girl vs. SECRETO VICTORIA (Lara Mashup 2024)", "Afterman", "mashup", "Lara"),
    ("Spark (Hardwell Remix)", "X", "remix", "Hardwell"),
])
def test_classify(title, artist, kind, remixer):
    v = classify(title, artist)
    assert v.kind == kind
    assert v.remixer == remixer


def test_generic_label_is_not_repeated():
    assert classify("Medusa (Remix)").label == ""
    assert classify("Pop, Lock & Drop It (Acappella)").label == ""
    assert classify("X (Acapella)").kind == "acapella" and classify("X (A Cappella)").label == ""
    assert classify("EoO (Afro House Remix)").label == "Afro House Remix"


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
        if fmt != "review":  # n'exporte que les morceaux douteux
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


# --- liens Spotify (MusicBrainz, API Spotify, song.link avec clé) -------------------------

def _links_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fast_mb(client):
    from mixscanner.spotify_links import MusicBrainz
    return MusicBrainz(client, interval=0)


def test_spotify_links_in_mix_order(monkeypatch):
    from mixscanner.models import PlatformResult
    from mixscanner.spotify_links import resolve_spotify_links

    for var in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "ODESLI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    asked = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host != "api.song.link"  # sans clé, song.link n'est jamais appelé
        if req.url.host == "musicbrainz.org":
            assert req.headers["User-Agent"].startswith("MixScanner/")
            asked.append(req.url.path)
            if req.url.path == "/ws/2/isrc/GBAAA0000001":
                return httpx.Response(200, json={"recordings": [{"relations": [
                    {"url": {"resource": "https://open.spotify.com/track/B"}}]}]})
            if req.url.path == "/ws/2/recording":
                return httpx.Response(200, json={"recordings": []})
            return httpx.Response(404)
        if req.url.host == "api.deezer.com":
            if "Rare" in str(req.url):
                return httpx.Response(200, json={"data": [{"title": "Rare Dub", "link": "https://www.deezer.com/track/7",
                                                           "artist": {"name": "Nobody"}, "isrc": "FRXXX0000009"}]})
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404)

    tracks = [
        _t("Glue", "Bicep", availability=[PlatformResult("spotify", "available", url="https://open.spotify.com/track/A")]),
        _t("Opus", "Eric Prydz", isrc="GBAAA0000001"),
        _t("ID", "ID"),
        _t("Rare Dub", "Nobody"),
        _t("Glue", "Bicep", links={"spotify": "https://open.spotify.com/track/A"}),
    ]
    progress = []

    async def run():
        async with _links_client(handler) as c:
            return await resolve_spotify_links(tracks, c, lambda d, t: progress.append((d, t)), musicbrainz=_fast_mb(c))

    links, missing, diag = asyncio.run(run())
    assert links == ["https://open.spotify.com/track/A", "https://open.spotify.com/track/B"]
    assert [t.title for t in missing] == ["Rare Dub"]
    assert "/ws/2/isrc/FRXXX0000009" in asked  # ISRC trouvé sur Deezer puis cherché sur MusicBrainz
    assert diag["found_by"] == {"déjà connu": 2, "MusicBrainz": 1} and diag["not_found"] == 1
    assert tracks[1].availability[0].platform == "spotify" and tracks[1].availability[0].status == "available"
    assert progress[-1] == (4, 4)


def test_spotify_links_musicbrainz_search_by_title(monkeypatch):
    from mixscanner.spotify_links import resolve_spotify_links

    for var in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "ODESLI_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "musicbrainz.org":
            if req.url.path == "/ws/2/recording":
                assert 'recording:"Umbrella (feat. JAY-Z)"' in req.url.params["query"]
                return httpx.Response(200, json={"recordings": [
                    {"id": "r1", "title": "Umbrella", "artist-credit": [{"name": "Rihanna", "joinphrase": ""}]}]})
            if req.url.path == "/ws/2/recording/r1":
                return httpx.Response(200, json={"relations": [
                    {"url": {"resource": "https://open.spotify.com/track/U"}}]})
        return httpx.Response(200, json={"data": []})

    async def run():
        async with _links_client(handler) as c:
            return await resolve_spotify_links([_t("Umbrella (feat. JAY-Z)", "Rihanna")], c, musicbrainz=_fast_mb(c))

    links, missing, diag = asyncio.run(run())
    assert links == ["https://open.spotify.com/track/U"] and missing == []


def test_spotify_links_odesli_key_refused_is_reported_once(monkeypatch):
    from mixscanner.models import PlatformResult
    from mixscanner.spotify_links import resolve_spotify_links

    monkeypatch.setenv("ODESLI_API_KEY", "k")
    for var in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    odesli_calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.song.link":
            odesli_calls.append(req.url.params["key"])
            return httpx.Response(401, json={"code": "PUBLIC_API_ACCESS_DEPRECATED"})
        if req.url.host == "musicbrainz.org":
            return httpx.Response(200, json={"recordings": []})
        return httpx.Response(200, json={"data": []})

    tracks = [_t(n, "X", availability=[PlatformResult("deezer", "available", url=f"https://www.deezer.com/track/{n}")])
              for n in ("One", "Two", "Three")]

    async def run():
        async with _links_client(handler) as c:
            return await resolve_spotify_links(tracks, c, musicbrainz=_fast_mb(c))

    links, missing, diag = asyncio.run(run())
    assert links == [] and len(missing) == 3
    assert odesli_calls == ["k"]  # clé refusée : pas de nouvel essai pour les morceaux suivants
    assert diag["errors"] == 1 and "ODESLI_API_KEY" in diag["first_error"]


def test_spotify_links_errors_are_reported(monkeypatch):
    from mixscanner.spotify_links import resolve_spotify_links

    for var in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "ODESLI_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    async def run():
        async with _links_client(lambda req: httpx.Response(500, text="boom")) as c:
            return await resolve_spotify_links([_t("Glue", "Bicep", isrc="GB0000000001")], c, musicbrainz=_fast_mb(c))

    links, missing, diag = asyncio.run(run())
    assert links == [] and diag["errors"] >= 1 and "500" in diag["first_error"]


# --- corrections : description, mashups, cache, limites de débit… ---------------------

def test_extracted_clip_is_a_valid_wav(mix_file):
    from mixscanner.audio import extract_clip
    wav = asyncio.run(extract_clip(mix_file, 5, 10))
    assert int.from_bytes(wav[4:8], "little") == len(wav) - 8  # taille RIFF exacte (pas 0xFFFFFFFF)
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        assert abs(w.getnframes() / 16000 - 10) < 0.1


def test_description_numbering_after_timestamp_is_removed():
    desc = "Tracklist:\n[00:00] 01. Bad Bunny - EoO (Bante Edit)\n[01:18] 02. Bante - Voy x3\n3:35 10) Foo - Bar"
    entries = parse_description(desc)
    assert [(e.start, e.artist, e.title) for e in entries] == [
        (0, "Bad Bunny", "EoO (Bante Edit)"), (78, "Bante", "Voy x3"), (215, "Foo", "Bar")]


def test_mashup_from_description_absorbs_its_parts():
    from mixscanner.tracklist import DescEntry
    tracks = build_tracklist([Hit(60, m("Who's da Gurl", "Disco Gurls")), Hit(90, m("SECRETO VICTORIA", "Fuerza Regida")),
                              Hit(120, m("SECRETO VICTORIA", "Fuerza Regida")), Hit(300, m("Voy", "Bante"))], 12, 400)
    entries = [DescEntry(78, "Afterman vs. Fuerza Regida", "Who's That Girl vs. SECRETO VICTORIA (Lara Mashup 2024)"),
               DescEntry(215, "Bante", "Voy")]
    out = merge_with_description(tracks, entries)
    assert [(t.title, t.start, t.source) for t in out] == [
        ("Who's That Girl vs. SECRETO VICTORIA (Lara Mashup 2024)", 60, "audio+description"),
        ("Voy", 300, "audio+description")]
    assert out[0].version.kind == "mashup" and out[0].hits == 3 and out[0].isrc is None
    assert [t.index for t in out] == [1, 2]


def test_build_tracklist_does_not_modify_hits():
    a1 = Match(title="A", artist="X", provider="t")
    a2 = Match(title="A", artist="X", provider="t", isrc="ISRC1", links={"deezer": "d"})
    build_tracklist([Hit(0, a1), Hit(30, a2)], 12)
    assert a1.isrc is None and a1.links == {}


def test_description_entries_outside_analyzed_range_are_ignored(mix_file, tmp_path, monkeypatch):
    from mixscanner import pipeline
    from mixscanner.audio import MixSource

    def fake_load(url, cache_dir, on_progress=None, cookies=None):
        return MixSource(path=mix_file, title="Mix", duration=260, cache_id="t",
                         description="00:00 Someone - First\n01:00 Someone - Second\n04:00 Someone - Late")
    monkeypatch.setattr(pipeline, "load_source", fake_load)
    opts = ScanOptions(step=20, clip=10, check_platforms=False, cache_dir=tmp_path, end=120)
    res = asyncio.run(scan("x", opts, recognizer=FrequencyRecognizer()))
    assert "Late" not in [t.title for t in res.tracks]
    assert "Second" in [t.title for t in res.tracks]


class CountingRecognizer(FrequencyRecognizer):
    def __init__(self):
        self.calls = 0

    async def recognize(self, wav):
        self.calls += 1
        return await super().recognize(wav)


def test_local_files_are_cached_and_cache_is_merged(mix_file, tmp_path):
    first = CountingRecognizer()
    asyncio.run(scan(str(mix_file), ScanOptions(step=20, clip=10, check_platforms=False, cache_dir=tmp_path, end=100),
                     recognizer=first))
    assert first.calls == 5
    second = CountingRecognizer()
    res = asyncio.run(scan(str(mix_file), ScanOptions(step=20, clip=10, check_platforms=False, cache_dir=tmp_path),
                           recognizer=second))
    assert second.calls == res.windows - 5  # seuls les nouveaux extraits sont analysés
    third = CountingRecognizer()
    asyncio.run(scan(str(mix_file), ScanOptions(step=20, clip=10, check_platforms=False, cache_dir=tmp_path, end=60),
                     recognizer=third))
    assert third.calls == 0


def test_itunes_rate_limit_is_retried():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.host)
        if len(calls) == 1:
            return httpx.Response(403, text="rate limited")
        return httpx.Response(200, json={"results": [
            {"artistName": "Bicep", "trackName": "Glue", "trackViewUrl": "https://music.apple.com/glue"}]})

    async def run():
        checker = PlatformChecker(httpx.AsyncClient(transport=httpx.MockTransport(handler)), itunes_interval=0)
        import mixscanner.platforms as pf
        orig = pf.asyncio.sleep

        async def fast_sleep(d):
            await orig(0)
        pf.asyncio.sleep = fast_sleep
        try:
            return await checker.apple_music(_t("Glue", "Bicep"))
        finally:
            pf.asyncio.sleep = orig

    res = asyncio.run(run())
    assert res.status == "available" and len(calls) == 2


def test_recognizer_account_errors_are_raised():
    with pytest.raises(RuntimeError, match="invalid access key"):
        ACRCloudRecognizer.parse({"status": {"code": 3001, "msg": "invalid access key"}})
    assert ACRCloudRecognizer.parse({"status": {"code": 1001, "msg": "No result"}}) is None
    from mixscanner.recognizers import AuddRecognizer
    with pytest.raises(RuntimeError, match="AudD"):
        AuddRecognizer.parse({"status": "error", "error": {"error_code": 901, "error_message": "limit reached"}})


def test_cli_rejects_bad_output_format_before_scanning(capsys):
    from mixscanner.cli import main
    with pytest.raises(SystemExit):
        main(["https://example.com/mix", "-o", "out.xls"])
    assert "Format non supporté" in capsys.readouterr().err


def test_download_errors_are_explained():
    from mixscanner.audio import explain_download_error
    msg = explain_download_error("ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies")
    assert "Cookies YouTube" in msg
    assert "pas installé" in explain_download_error('ERROR: could not find chrome cookies database in "/x"')


def test_web_validation_and_spotify_links_started_once(monkeypatch):
    from fastapi.testclient import TestClient
    from mixscanner import web

    client = TestClient(web.app)
    assert client.post("/api/scan", json={"url": "x", "recognizer": "nope"}).status_code == 422
    assert client.post("/api/scan", json={"url": "x", "cookies_from_browser": "netscape"}).status_code == 422

    started = []
    monkeypatch.setattr(web.Job, "resolve_spotify_links", lambda self: started.append(1))
    job = web.Job(web.ScanRequest(url="x"))
    job.result = _result_with([_t("Glue", "Bicep")])
    web.JOBS[job.id] = job
    assert job.start_spotify_links() is True
    assert client.post(f"/api/jobs/{job.id}/spotify-links").status_code == 200  # déjà en cours : rien relancé
    import time
    time.sleep(0.1)
    assert len(started) == 1


# --- versions copiées et liste « propre » pour Spotlistr ---------------------------------

def test_lookalike_versions_next_to_each_other_are_merged():
    hits = [Hit(0, m("Last Last (DJ Stavo Amapiano Remix) [Mixed]", "Burna Boy")),
            Hit(30, m("Last Last (Afro Dancehall)", "SKL SOUND")),
            Hit(60, m("A Box", "Drxse")), Hit(90, m("The Box", "Roddy Ricch")),
            Hit(120, m("If", "Davido"))]
    tracks = build_tracklist(hits, 12, 200)
    assert [(t.artist, t.start) for t in tracks] == [("Burna Boy", 0), ("Drxse", 60), ("Davido", 120)]
    assert [a.artist for a in tracks[0].alternatives] == ["SKL SOUND"]
    assert tracks[1].ambiguous and tracks[1].needs_review and tracks[1].confidence == "low"
    assert not tracks[2].alternatives


def test_original_then_remix_both_played_are_kept():
    hits = [Hit(o, m("Opus", "Eric Prydz")) for o in (0, 30, 60)] + \
           [Hit(o, m("Opus (Four Tet Remix)", "Eric Prydz")) for o in (90, 120)]
    assert len(build_tracklist(hits, 12, 200)) == 2


def test_most_popular_version_is_kept_when_shazam_hesitates():
    from mixscanner.pipeline import pick_most_popular

    tracks = build_tracklist([Hit(60, m("A Box", "Drxse")), Hit(90, m("The Box", "Roddy Ricch"))], 12, 200)

    class Pop:
        async def deezer_popularity(self, artist, title):
            return {"Drxse": 1200, "Roddy Ricch": 950000}[artist]

    asyncio.run(pick_most_popular(tracks, Pop()))
    assert (tracks[0].artist, tracks[0].title) == ("Roddy Ricch", "The Box")
    assert [a.artist for a in tracks[0].alternatives] == ["Drxse"]


def test_playlist_lines_are_easy_to_find_on_spotify():
    from mixscanner.export import playlist_line
    from mixscanner.models import PlatformResult

    dz = lambda title, same=True: [PlatformResult("deezer", "available", matched_title=title, same_version=same)]
    assert playlist_line(_t("F**k Up Some Commas", "Future", availability=dz("Future - Fuck Up Some Commas"))) \
        == "Future - Fuck Up Some Commas"
    assert playlist_line(_t("Ni**as in Paris", "JAŸ-Z & Kanye West")) == "JAŸ-Z - Ni**as in Paris"
    assert playlist_line(_t("Umbrella (feat. JAŸ-Z) [Mixed]", "Rihanna")) == "Rihanna - Umbrella"
    assert playlist_line(_t("Swag Surfin'", "F.L.Y. - Fast Life Yungstaz")) == "F.L.Y. - Swag Surfin'"
    assert playlist_line(_t("Tahara - Tokyo Drift", "Tahara (Br)")) == "Tahara - Tahara - Tokyo Drift"
    assert playlist_line(_t("Mercy (feat. Big Sean, Pusha T & 2 Chainz)", "Kanye West")) == "Kanye West - Mercy"
    # remix joué mais seul l'original existe sur Deezer : on garde le nom du remix
    assert playlist_line(_t("Party In the U.S.A. (JWeezy Remix)", "Miley Cyrus",
                            availability=dz("Miley Cyrus - Party In the U.S.A.", same=False))) \
        == "Miley Cyrus - Party In the U.S.A. (JWeezy Remix)"
    res = _result_with([_t("Umbrella (feat. JAŸ-Z) [Mixed]", "Rihanna"), _t("Umbrella", "Rihanna")])
    assert EXPORTERS["playlist"](res) == "Rihanna - Umbrella\n"


def test_karaoke_and_other_artists_are_not_accepted_as_matches():
    from mixscanner.platforms import _best
    t = _t("Ni**as in Paris", "JAŸ-Z & Kanye West")
    cands = [("Hits 1 Karaoke", "Ni**as in Paris (Originally by Jay-Z & Kanye West)", "k"),
             ("Somebody Else", "Ni**as in Paris", "x")]
    assert _best(t, cands) is None
    assert _best(t, cands + [("JAY-Z", "Ni**as in Paris", "ok")])[1] == "ok"
    assert _best(_t("Swag Surfin'", "F.L.Y. - Fast Life Yungstaz"),
                 [("F.L.Y. (Fast Life Yungstaz)", "Swag Surfin'", "fly")])[1] == "fly"


def test_quit_only_available_in_mac_app(monkeypatch):
    from fastapi.testclient import TestClient
    from mixscanner import web

    client = TestClient(web.app)
    monkeypatch.delenv("MIXSCANNER_APP", raising=False)
    assert client.get("/api/app").json()["app"] is False
    assert client.post("/api/quit").status_code == 404
    monkeypatch.setenv("MIXSCANNER_APP", "1")
    stopped = []
    monkeypatch.setattr(web, "_quit_callbacks", [lambda: stopped.append(1)])
    assert client.get("/api/app").json()["app"] is True
    monkeypatch.setenv("MIXSCANNER_WINDOW", "1")
    assert client.get("/api/app").json()["window"] is True
    assert client.post("/api/quit").status_code == 200 and stopped == [1]
    assert web.idle_seconds() < 5
