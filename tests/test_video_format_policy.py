"""Pure, no-network tests for the YouTube video-format candidate policy.

Imports main with soco/mpv/yt_dlp stubbed out (same harness as
test_stream_session.py) and exercises rank_video_format_candidates() and
its tier/codec/fps/bitrate policy directly. Nothing touches the network,
the resolver, or playback -- resolve_media_url()/_probe_url_audio_tracks()
still select by greatest height; wiring candidates into them is owned by a
later todo. Covers:

1. Tier order (2160 -> 1440 -> 1080) and at most one candidate per tier.
2. Invalid entries are excluded: audio-only/missing/empty codecs, missing
   or empty URLs, missing/zero/negative heights, heights below 1080, and
   heights above the 4K/2160p cap (no 8K bucket).
3. Within-tier ordering: GPU-likely codec families (avc/h264, then vp9,
   then av01/av1, then other), lower fps before higher fps (30 before 60,
   unknown fps last), then higher tbr, then greater height/width.
4. HDR metadata comes from format_is_hdr() and results are deterministic
   and immutable across input permutations and repeated calls.
"""

import dataclasses
import random
import unittest

from _import_main import import_main_no_network as _import_main_no_network


def _fmt(fmt_id="v", url="http://cdn.example/video", vcodec="avc1.640028",
         height=1080, width=1920, fps=30, tbr=5000.0, **extra):
    """Build a minimal yt-dlp-style video format entry."""
    fmt = {
        "format_id": fmt_id,
        "url": url,
        "vcodec": vcodec,
        "acodec": "none",
        "height": height,
        "width": width,
        "fps": fps,
        "tbr": tbr,
    }
    fmt.update(extra)
    return fmt


class VideoFormatCandidatePolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _import_main_no_network()

    def ranks(self, formats):
        return self.main.rank_video_format_candidates(formats)

    # -- tiering ----------------------------------------------------------

    def test_tier_order_is_2160_1440_1080(self):
        cands = self.ranks([
            _fmt("a1080", height=1080),
            _fmt("a1440", height=1440),
            _fmt("a2160", height=2160),
        ])
        self.assertEqual([c.tier for c in cands], ["2160", "1440", "1080"])
        self.assertEqual([c.format_id for c in cands], ["a2160", "a1440", "a1080"])

    def test_one_candidate_per_tier(self):
        cands = self.ranks([
            _fmt("vp9-2160-60", vcodec="vp9", height=2160, fps=60, tbr=25000),
            _fmt("avc-2160-30", vcodec="avc1.640028", height=2160, fps=30, tbr=15000),
            _fmt("vp9-1440-60", vcodec="vp9", height=1440, fps=60, tbr=16000),
            _fmt("vp9-1440-30", vcodec="vp9", height=1440, fps=30, tbr=9000),
            _fmt("avc-1080", vcodec="avc1.640028", height=1080, fps=30, tbr=4500),
        ])
        # Codec family outranks fps, so AVC-30 wins 2160; fps outranks tbr,
        # so the 30fps VP9 wins 1440 even at lower bitrate.
        self.assertEqual([c.format_id for c in cands],
                         ["avc-2160-30", "vp9-1440-30", "avc-1080"])
        self.assertEqual([c.tier for c in cands], ["2160", "1440", "1080"])

    def test_tier_boundaries_and_out_of_window_tiers_excluded(self):
        cases = [
            (719, None), (1079, None), (1080, "1080"), (1439, "1080"),
            (1440, "1440"), (2159, "1440"), (2160, "2160"),
            (2161, None), (2880, None), (4320, None),
        ]
        for height, expected in cases:
            with self.subTest(height=height):
                cands = self.ranks([_fmt(f"h{height}", height=height)])
                if expected is None:
                    self.assertEqual(cands, [])
                else:
                    self.assertEqual([c.tier for c in cands], [expected])

    def test_above_4k_never_selected_even_with_preferred_ranking(self):
        # 4K/2160p is the cap: a 4320p source with the more-preferred codec
        # (AVC beats VP9), lower fps (30 beats 60), and higher bitrate must
        # not displace a 2160p candidate, and never appears in the results.
        avc_4320 = _fmt("avc-4320", vcodec="avc1.640028", height=4320,
                        width=7680, fps=30, tbr=40000)
        vp9_2160 = _fmt("vp9-2160", vcodec="vp9", height=2160,
                        width=3840, fps=60, tbr=15000)
        self.assertEqual(self.ranks([avc_4320]), [])
        cands = self.ranks([avc_4320, vp9_2160])
        self.assertEqual([c.format_id for c in cands], ["vp9-2160"])
        self.assertEqual([c.tier for c in cands], ["2160"])

    # -- invalid input ----------------------------------------------------

    def test_invalid_formats_excluded(self):
        invalid = [
            _fmt("audio-only", vcodec="none"),        # audio-only format
            _fmt("empty-codec", vcodec=""),           # no usable video codec
            {"format_id": "no-codec", "url": "http://cdn.example/v",
             "height": 1080},                          # vcodec missing entirely
            _fmt("no-url", url=None),  # type: ignore
            _fmt("empty-url", url=""),
            _fmt("no-height", height=None),  # type: ignore
            _fmt("zero-height", height=0),
            _fmt("negative-height", height=-1080),
            _fmt("below-floor", height=720),
            "not-a-dict",
        ]
        self.assertEqual(self.ranks(invalid), [])
        self.assertEqual(self.ranks(None), [])
        self.assertEqual(self.ranks([]), [])
        # Invalid entries never displace valid ones in the same tier.
        cands = self.ranks(invalid + [_fmt("ok", height=1080)])
        self.assertEqual([c.format_id for c in cands], ["ok"])

    # -- within-tier ordering ---------------------------------------------

    def test_codec_family_ordering_within_tier(self):
        avc = _fmt("avc", vcodec="avc1.640028", height=1440, fps=60, tbr=3000)
        h264 = _fmt("h264", vcodec="h264", height=1440, fps=60, tbr=3001)
        vp9 = _fmt("vp9", vcodec="vp9", height=1440, fps=60, tbr=3000)
        vp09 = _fmt("vp09", vcodec="vp09.00.10.08", height=1440, fps=60, tbr=3000)
        av01 = _fmt("av01", vcodec="av01.0.08M.08", height=1440, fps=60, tbr=3000)
        av1 = _fmt("av1", vcodec="av1", height=1440, fps=60, tbr=3000)
        other = _fmt("hevc", vcodec="hevc", height=1440, fps=60, tbr=3000)

        self.assertEqual(self.ranks([vp9, avc])[0].format_id, "avc")
        self.assertEqual(self.ranks([av01, vp9])[0].format_id, "vp9")
        # Both AV1 spellings share one family: fully tied, first seen wins.
        self.assertEqual(self.ranks([av01, av1])[0].format_id, "av01")
        self.assertEqual(self.ranks([av1, av01])[0].format_id, "av1")
        self.assertEqual(self.ranks([other, av01])[0].format_id, "av01")
        # Both VP9 spellings share one family: fully tied, first seen wins.
        self.assertEqual(self.ranks([vp09, vp9])[0].format_id, "vp09")
        self.assertEqual(self.ranks([vp9, vp09])[0].format_id, "vp9")
        self.assertEqual(self.ranks([h264, vp9, av01, other])[0].format_id, "h264")
        # "other" codecs are still eligible, just ranked last.
        self.assertEqual(self.ranks([other])[0].format_id, "hevc")

    def test_lower_fps_preferred_within_codec_and_tier(self):
        f60 = _fmt("avc-60", vcodec="avc1", height=1080, fps=60, tbr=9000)
        f30 = _fmt("avc-30", vcodec="avc1", height=1080, fps=30, tbr=4000)
        cands = self.ranks([f60, f30])
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].format_id, "avc-30")
        self.assertEqual(cands[0].fps, 30.0)

    def test_unknown_fps_ranks_after_known_fps(self):
        unknown = _fmt("no-fps", height=1080, fps=None)  # type: ignore
        f60 = _fmt("fps-60", height=1080, fps=60)
        cands = self.ranks([unknown, f60])
        self.assertEqual(cands[0].format_id, "fps-60")
        # Unknown fps is stored as 0.0 in the record but ranks last.
        self.assertEqual(cands[0].fps, 60.0)
        self.assertEqual(self.ranks([unknown])[0].fps, 0.0)

    def test_higher_bitrate_wins_when_codec_and_fps_tie(self):
        low = _fmt("low", vcodec="vp9", height=1080, fps=30, tbr=3000)
        high = _fmt("high", vcodec="vp9", height=1080, fps=30, tbr=6000)
        self.assertEqual(self.ranks([low, high])[0].format_id, "high")
        self.assertEqual(self.ranks([high, low])[0].format_id, "high")

    def test_height_then_width_tie_break(self):
        # Both inside the 1440 tier (height < 2160): taller wins.
        shorter = _fmt("h1440", height=1440, tbr=5000)
        taller = _fmt("h2159", height=2159, tbr=5000)
        self.assertEqual(self.ranks([taller, shorter])[0].format_id, "h2159")
        # Same height and bitrate: wider wins.
        narrow = _fmt("narrow", height=1080, width=1280)
        wide = _fmt("wide", height=1080, width=1920)
        self.assertEqual(self.ranks([wide, narrow])[0].format_id, "wide")

    def test_fully_tied_entry_keeps_first_seen(self):
        first = _fmt("first", height=1080)
        second = _fmt("second", height=1080)
        self.assertEqual(self.ranks([first, second])[0].format_id, "first")
        self.assertEqual(self.ranks([second, first])[0].format_id, "second")

    # -- metadata ---------------------------------------------------------

    def test_hdr_metadata_reflects_format_is_hdr(self):
        main = self.main
        hdr_variants = [
            {"dynamic_range": "HDR"},
            {"color_transfer": "smpte2084"},
            {"color_primaries": "bt2020"},
            {"format_note": "2160p HDR"},
        ]
        for extra in hdr_variants:
            with self.subTest(extra=extra):
                cand = self.ranks([_fmt("h", height=2160, **extra)])[0]
                self.assertTrue(cand.is_hdr)
                self.assertEqual(cand.is_hdr, main.format_is_hdr(_fmt("h", height=2160, **extra)))
        self.assertFalse(self.ranks([_fmt("s", height=2160)])[0].is_hdr)
        # HDR is metadata only: it never outranks a better-ranked SDR format.
        hdr_vp9 = _fmt("hdr-vp9", vcodec="vp9", height=2160, fps=60, dynamic_range="HDR")
        sdr_avc = _fmt("sdr-avc", vcodec="avc1", height=2160, fps=30)
        cands = self.ranks([hdr_vp9, sdr_avc])
        self.assertEqual([c.format_id for c in cands], ["sdr-avc"])
        self.assertFalse(cands[0].is_hdr)

    def test_http_headers_carried_and_copied(self):
        fmt = _fmt("with-headers", height=1080, http_headers={"User-Agent": "ua/1"})
        cand = self.ranks([fmt])[0]
        self.assertEqual(cand.http_headers, {"User-Agent": "ua/1"})
        # noinspection PyUnresolvedReferences
        fmt["http_headers"]["User-Agent"] = "ua/2"  # source mutation must not leak
        self.assertEqual(cand.http_headers, {"User-Agent": "ua/1"})
        self.assertEqual(self.ranks([_fmt("bare", height=1080)])[0].http_headers, {})

    # -- determinism / immutability ----------------------------------------

    def test_deterministic_across_input_order_and_repeats(self):
        formats = [
            _fmt("avc-2160", vcodec="avc1.640028", height=2160, fps=30, tbr=18000),
            _fmt("vp9-2160", vcodec="vp9", height=2160, fps=60, tbr=24000),
            _fmt("avc-1440", vcodec="avc1.640028", height=1440, fps=30, tbr=9000),
            _fmt("vp9-1440", vcodec="vp9", height=1440, fps=60, tbr=12000),
            _fmt("avc-1080", vcodec="avc1.640028", height=1080, fps=30, tbr=4500),
            _fmt("vp9-1080", vcodec="vp9", height=1080, fps=30, tbr=5000),
            _fmt("av01-1080", vcodec="av01.0.08M.08", height=1080, fps=60, tbr=8000),
            _fmt("audio", vcodec="none"),   # excluded
            _fmt("small", height=720),      # excluded
        ]
        expected = self.ranks(formats)
        self.assertEqual([c.format_id for c in expected],
                         ["avc-2160", "avc-1440", "avc-1080"])
        rng = random.Random(1234)
        for _ in range(10):
            shuffled = formats[:]
            rng.shuffle(shuffled)
            self.assertEqual(self.ranks(shuffled), expected)
        self.assertEqual(self.ranks(formats), self.ranks(formats))

    def test_candidates_are_frozen_records(self):
        cand = self.ranks([_fmt("only", height=1080, url="http://cdn.example/x")])[0]
        self.assertEqual(cand.url, "http://cdn.example/x")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cand.tier = "1440"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cand.url = "http://cdn.example/other"

    def test_describe_helper_is_pure_string(self):
        main = self.main
        cand = main.rank_video_format_candidates(
            [_fmt("d1", height=1440, width=2560, fps=30, tbr=9000, vcodec="vp9")])[0]
        text = main.describe_video_format_candidate(cand)
        self.assertIsInstance(text, str)
        for token in ("1440p", "d1", "2560x1440", "30fps", "vp9", "9000kbps", "hdr=False"):
            self.assertIn(token, text)
        # Unknown fps/tbr are shown as placeholders, and output is stable.
        unknown = main.rank_video_format_candidates(
            [_fmt("u", height=1080, fps=None, tbr=None)])[0]  # type: ignore
        self.assertIn("@?fps", main.describe_video_format_candidate(unknown))
        self.assertIn("?kbps", main.describe_video_format_candidate(unknown))
        self.assertEqual(main.describe_video_format_candidate(unknown),
                         main.describe_video_format_candidate(unknown))


if __name__ == "__main__":
    unittest.main()
