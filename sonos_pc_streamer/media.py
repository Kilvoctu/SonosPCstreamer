"""Media resolution, probing, and format policy for Sonos PC Streamer.

Extracted from main.py: yt-dlp URL resolution, HTML page scraping,
ffprobe audio-track probing, HDR transfer detection, ranked video-format
candidates, and the video startup health verdict. UI-free on purpose (no
Qt, no mpv, no soco) -- dependency direction is media -> config ->
logging_utils only.
"""

import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import yt_dlp

from sonos_pc_streamer.config import CREATE_NO_WINDOW, FFPROBE_PATH
from sonos_pc_streamer.logging_utils import timestamped_print as _print

_last_yt_error = None


def get_last_yt_error():
    """Latest yt-dlp failure code recorded by resolve/probe (None if the
    last resolve/probe attempt succeeded)."""
    return _last_yt_error


def _classify_yt_error(err):
    """Return a short code for common yt-dlp failures, or None."""
    msg = str(err).lower()
    if "confirm your age" in msg or "sign in to confirm your age" in msg \
       or "you must confirm your age" in msg or "is age restricted" in msg:
        return "age"
    if "private video" in msg or "private" in msg and "video" in msg:
        return "private"
    if "not available in your country" in msg or "geo" in msg or "in your country" in msg:
        return "geo"
    if "unavailable" in msg or "removed" in msg or "couldn't be found" in msg:
        return "unavailable"
    if "confirm you're not a bot" in msg or "sign in to confirm you're not a bot" in msg or "bot check" in msg:
        return "bot"
    if "javascript runtime" in msg or "js runtime" in msg:
        return "js_runtime"
    return None


def yt_error_text(code):
    if code == "age":
        return "age-restricted video — YouTube requires sign-in to confirm your age"
    if code == "private":
        return "private video (not accessible)"
    if code == "geo":
        return "video isn't available in your region"
    if code == "unavailable":
        return "video is unavailable or was removed"
    if code == "bot":
        return "video hit a YouTube bot check (see logs)"
    if code == "js_runtime":
        return "YouTube needs a JavaScript runtime for extraction (see yt-dlp logs)"
    return "could not be resolved"


_stderr_filter_lock = threading.Lock()


class _YtdlpDeprecationFilter:

    _PREFIX = "Deprecated Feature:"

    def __init__(self):
        self._original = None
        self._depth = 0

    def write(self, text):
        if self._original is not None and not text.startswith(self._PREFIX):
            self._original.write(text)
        return len(text)

    def flush(self):
        if self._original is not None:
            self._original.flush()

    def __enter__(self):
        with _stderr_filter_lock:
            if self._original is None:
                self._original = sys.stderr
                sys.stderr = self
            self._depth += 1
        return self

    def __exit__(self, *exc):
        with _stderr_filter_lock:
            self._depth -= 1
            if self._depth <= 0:
                if self._original is not None and sys.stderr is self:
                    sys.stderr = self._original
                self._original = None
                self._depth = 0
        return False


_ytdlp_stderr_filter = _YtdlpDeprecationFilter()


def resolve_media_url(url):
    """Resolve a media URL to direct video + audio CDN URLs.

    Returns (video_url, audio_url, title, duration, is_live, video_headers,
    audio_headers, video_candidates) or
    (None, None, None, 0, False, {}, {}, []) on failure. ``video_candidates``
    is the ranked tier list from rank_video_format_candidates() over the
    available formats ([] when formats are missing or unrankable); the
    video_url/video_headers selection is unchanged.
    """
    global _last_yt_error
    _last_yt_error = None
    try:
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }
        # noinspection PyTypeChecker
        with _ytdlp_stderr_filter, yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None or not info:
                _print("[RESOLVE] Failed to extract info")
                return None, None, None, 0, False, {}, {}, []
            video_url = info.get("url")
            audio_url = info.get("url")
            title = info.get("title") or "Unknown"
            duration = info.get("duration", 0) or 0
            is_live = info.get("is_live", False) or False
            video_headers = {}
            audio_headers = {}
            if "formats" in info:
                formats = info["formats"] or []
                best_audio = None
                best_audio_key = None
                for fmt in formats:
                    if fmt.get("acodec", "none") != "none" and fmt.get("vcodec", "none") == "none":
                        key = _audio_sort_key(fmt)
                        if best_audio is None or key < best_audio_key:
                            best_audio = fmt
                            best_audio_key = key
                if best_audio:
                    _print(f"[RESOLVE] Audio pick: {best_audio.get('format_id') or '?'} "
                          f"({best_audio.get('abr') or '?'}kbps, lang={best_audio.get('language') or '?'})")
                audio_headers = best_audio.get("http_headers", {}) if best_audio else {}
                if best_audio:
                    audio_url = best_audio.get("url", audio_url)
                best_video = None
                for fmt in formats:
                    if (fmt.get("vcodec", "none") != "none" and fmt.get("height", 0)
                            and (best_video is None
                                 or (fmt.get("height", 0) or 0) > (best_video.get("height", 0) or 0))):
                        best_video = fmt
                if best_video:
                    video_url = best_video.get("url", video_url)
                    video_headers = best_video.get("http_headers", {})
                else:
                    video_headers = {}
            video_candidates = rank_video_format_candidates(info.get("formats", []))
            if video_candidates:
                _print("[RESOLVE] Video candidates: " + ", ".join(
                    f"{c.tier}:{c.vcodec.split('.')[0]} {(f'{c.fps:g}' if c.fps else '?')}fps"
                    for c in video_candidates))
            _print(f"[RESOLVE] Resolved: {title} (live={is_live}, dur={duration}s)")
            return (video_url, audio_url, title, duration, is_live,
                    video_headers, audio_headers, video_candidates)
    except (Exception,) as e:
        _print(f"[RESOLVE] Resolution failed: {e}")
        _last_yt_error = _classify_yt_error(e)
        return None, None, None, 0, False, {}, {}, []


def scrape_page_for_media(url):
    """Fetch a page and extract a playable media URL from its HTML.

    Handles JS-rendered video players that yt-dlp cannot parse.
    Returns a direct media URL string, an iframe embed URL for further
    scraping, or None if nothing is found.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # JS config patterns: "file":"..." or "src":"..."
        file_urls = re.findall(r'"file"\s*:\s*"([^"]+)"', html)
        src_urls = re.findall(r'"src"\s*:\s*"([^"]+)"', html)
        video_src = re.findall(r'<video[^>]+src="([^"]+)"', html, re.IGNORECASE)
        source_src = re.findall(r'<source[^>]+src="([^"]+)"', html, re.IGNORECASE)
        iframes = re.findall(r'<iframe[^>]+src="([^"]+)"', html, re.IGNORECASE)

        # Collect candidates in priority order
        candidates = file_urls + src_urls + video_src + source_src + iframes

        media_ext = re.compile(r'\.(m3u8|mp4|webm|ts)(\?|$)', re.IGNORECASE)

        for c in candidates:
            c = c.strip().strip('"\'')
            absolute: str = urljoin(url, c)  # type: ignore
            if media_ext.search(absolute):
                _print(f"[SCRAPE] Found media URL: {absolute[:120]}")
                return absolute

        # No direct media URL; return first iframe for recursive scraping
        if iframes:
            iframe_url: str = urljoin(url, iframes[0].strip().strip('"\''))  # type: ignore
            _print(f"[SCRAPE] Found iframe embed: {iframe_url}")
            return iframe_url

        _print(f"[SCRAPE] No media found in {url[:80]}")
        return None
    except (Exception,) as e:
        _print(f"[SCRAPE] Failed to fetch {url[:80]}: {e}")
        return None


def probe_audio_tracks(path_or_url):
    """Probe a local file or URL for available audio tracks.

    Returns a dict with probe results, or None on failure.
    For local files: {"type": "local", "audio_tracks": [...]}
    For URLs: {"type": "url", "video_url": ..., "title": ..., "duration": ...,
               "is_live": ..., "video_headers": ..., "video_candidates": [...],
               "audio_tracks": [...]}
    """
    parsed = urlparse(path_or_url)
    is_url = parsed.scheme in ("http", "https", "ftp", "udp", "rtmp", "rtsp")
    if is_url:
        return _probe_url_audio_tracks(path_or_url)
    else:
        return _probe_local_audio_tracks(path_or_url)


def _is_hdr_transfer(color_transfer):
    """Check if a color_transfer value indicates HDR content."""
    if not color_transfer:
        return False
    ct = color_transfer.lower()
    return ct in ("smpte2084", "arib-std-b67", "smpte428", "pq", "hlg")


def format_is_hdr(fmt):
    """Return True if a yt-dlp format entry indicates HDR video."""
    if _is_hdr_transfer(fmt.get("color_transfer", "")):
        return True
    prim = (fmt.get("color_primaries") or "").lower()
    if prim in ("bt2020", "bt2020-10", "bt2020-12"):
        return True
    dr = (fmt.get("dynamic_range") or "").lower()
    if "hdr" in dr or "hlg" in dr or "pq" in dr:
        return True
    note = (fmt.get("format_note") or "").lower()
    return "hdr" in note


# Ranked YouTube video-format candidates for smooth-playback fallback
# (2160p -> 1440p -> 1080p; 4K is the maximum selected resolution and
# above-4K sources are excluded). Pure metadata + selection helpers for
# now; resolve_media_url()/_probe_url_audio_tracks() still pick by
# greatest height and are wired to this policy by a later todo.

_VIDEO_TIER_ORDER = ("2160", "1440", "1080")

# Cap on selectable source height: 4K/2160p. Formats taller than this
# (e.g. 8K/4320p) are never chosen as candidates.
_VIDEO_MAX_HEIGHT = 2160


@dataclass(frozen=True)
class VideoFormatCandidate:
    """Immutable metadata for one ranked YouTube video-format candidate.

    At most one candidate per tier ("2160", "1440", "1080"); 2160p/4K is
    the maximum selectable tier. Numeric fields are coerced: unknown
    width/fps/tbr are stored as 0. ``is_hdr`` comes from format_is_hdr().
    """

    format_id: str
    url: str
    http_headers: dict
    height: int
    width: int
    fps: float
    vcodec: str
    tbr: float
    is_hdr: bool
    tier: str


def _video_tier_for_height(height):
    """Map a video height to its fallback tier name, or None below 1080p
    or above the 4K/2160p cap (above-4K sources get no tier/bucket)."""
    if height > _VIDEO_MAX_HEIGHT:
        return None
    if height >= 2160:
        return "2160"
    if height >= 1440:
        return "1440"
    if height >= 1080:
        return "1080"
    return None


def _video_codec_family_rank(vcodec):
    """GPU-likely codec ordering: AVC/H.264 < VP9 < AV1 < everything else."""
    v = (vcodec or "").lower()
    if "avc" in v or "h264" in v:
        return 0
    if "vp9" in v or "vp09" in v:
        return 1
    if "av01" in v or "av1" in v:
        return 2
    return 3


def _video_candidate_sort_key(fmt):
    """Within-tier ranking key (lower sorts better).

    Reliability-first: GPU-likely codec family, then lower fps (30 before
    60; unknown fps ranks last), then higher bitrate, then greater
    height/width as deterministic tie-breakers. Heuristic only - no promise
    of hardware decode; the later health check verifies actual playback.
    """
    fps = fmt.get("fps")
    fps = float(fps) if isinstance(fps, (int, float)) else float("inf")
    tbr = fmt.get("tbr")
    tbr = float(tbr) if isinstance(tbr, (int, float)) else 0.0
    width = fmt.get("width")
    width = int(width) if isinstance(width, (int, float)) else 0
    return (
        _video_codec_family_rank(fmt.get("vcodec")),
        fps,
        -tbr,
        -(fmt.get("height") or 0),
        -width,
    )


def rank_video_format_candidates(formats):
    """Return ranked video-format candidates, at most one per tier.

    Pure selection over yt-dlp ``formats`` entries, ordered 2160 -> 1440 ->
    1080, capped at 4K: entries taller than 2160p (e.g. 8K/4320p) are never
    selected, while normal 2160p stays eligible. Ignores entries without a
    video codec, a URL, or a positive height, and never returns tiers below
    1080 or above 2160. Does not log or mutate. Fully-tied entries keep the
    first one seen in ``formats``.
    """
    best = {}
    for fmt in formats or ():
        if not isinstance(fmt, dict):
            continue
        if (fmt.get("vcodec") or "none") == "none":
            continue
        url = fmt.get("url") or ""
        if not url:
            continue
        height = fmt.get("height")
        if not isinstance(height, (int, float)) or height <= 0:
            continue
        tier = _video_tier_for_height(height)
        if tier is None:
            continue
        key = _video_candidate_sort_key(fmt)
        current = best.get(tier)
        # noinspection PyUnresolvedReferences
        if current is not None and key >= current[0]:
            continue
        width = fmt.get("width")
        fps = fmt.get("fps")
        tbr = fmt.get("tbr")
        best[tier] = (key, VideoFormatCandidate(
            format_id=f"{fmt.get('format_id', '')}",
            url=url,
            http_headers=dict(fmt.get("http_headers") or {}),
            height=int(height),
            width=int(width) if isinstance(width, (int, float)) else 0,
            fps=float(fps) if isinstance(fps, (int, float)) else 0.0,
            vcodec=f"{fmt.get('vcodec') or ''}",
            tbr=float(tbr) if isinstance(tbr, (int, float)) else 0.0,
            is_hdr=format_is_hdr(fmt),
            tier=tier,
        ))
    return [best[tier][1] for tier in _VIDEO_TIER_ORDER if tier in best]


def describe_video_format_candidate(candidate):
    """Concise one-line candidate description for log lines."""
    fps = f"{candidate.fps:g}" if candidate.fps else "?"
    tbr = f"{candidate.tbr:.0f}kbps" if candidate.tbr else "?kbps"
    size = f"{candidate.width}x{candidate.height}" if candidate.width else f"{candidate.height}p"
    return (f"{candidate.tier}p id={candidate.format_id} {size}@{fps}fps "
            f"{candidate.vcodec} {tbr} hdr={candidate.is_hdr}")


def video_health_verdict(hwdec, tier, frame_drop_delta, cache_pause_ratio):
    """Return (healthy: bool, reason: str) for a video tier startup sample.

    4K/2160 requires active hardware decode; lower tiers tolerate software
    decode. Sustained frame drops or dominant cache pauses are unhealthy.

    Thresholds, evaluated over the ~3.0s startup window sampled by
    MainWindow._check_video_startup_health (first matching rule wins):
    - ``hwdec`` in (None, "", "no") with tier "2160": software-decoded 4K
      cannot keep up -> (False, "no hardware decode for 4K").
    - ``frame_drop_delta`` > 45 dropped frames within the ~3s sample
      (>~15 drops/s sustained) -> (False, "sustained frame drops");
      exactly 45 is tolerated.
    - ``cache_pause_ratio`` > 0.5 (more than half of the 0.5s
      paused-for-cache samples spent stalled) -> (False, "repeated cache
      underruns"); exactly 0.5 is tolerated.
    """
    if (hwdec is None or hwdec == "" or hwdec == "no") and tier == "2160":
        return False, "no hardware decode for 4K"
    if frame_drop_delta > 45:
        return False, "sustained frame drops"
    if cache_pause_ratio > 0.5:
        return False, "repeated cache underruns"
    return True, ""


def _probe_local_audio_tracks(file_path):
    if not os.path.isfile(file_path):
        return None
    ffprobe_path = FFPROBE_PATH
    if not ffprobe_path:
        return None
    try:
        cmd = [ffprobe_path, "-v", "quiet", "-print_format", "json",
               "-show_streams", "-select_streams", "a", file_path]
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL,
                                      creationflags=CREATE_NO_WINDOW)
        data = json.loads(out)
        streams = data.get("streams", [])
        tracks = []
        for s in streams:
            idx = s.get("index", 0)
            codec = s.get("codec_name", "unknown")
            ch_layout = s.get("channel_layout", "")
            tags = s.get("tags", {}) or {}
            title = tags.get("title", "")
            lang = tags.get("language", "")
            display_parts = []
            if title:
                display_parts.append(title)
            elif lang:
                display_parts.append(lang.upper())
            else:
                display_parts.append(f"Stream {idx}")
            codec_part = codec
            if ch_layout:
                codec_part += f", {ch_layout}"
            display_parts.append(f"({codec_part})")
            display = " ".join(display_parts)
            tracks.append({
                "index": idx,
                "title": display,
                "codec": codec,
                "channels": ch_layout,
                "language": lang,
                "_raw_title": title,
            })
        source_hdr = False
        try:
            vcmd = [ffprobe_path, "-v", "quiet", "-print_format", "json",
                    "-show_streams", "-select_streams", "v", file_path]
            vout = subprocess.check_output(vcmd, text=True, stderr=subprocess.DEVNULL,
                                           creationflags=CREATE_NO_WINDOW)
            vdata = json.loads(vout)
            for vs in vdata.get("streams", []):
                ct = vs.get("color_transfer", "") or ""
                if _is_hdr_transfer(ct):
                    source_hdr = True
                    break
        except (Exception,) as exc:
            _print(f"[PROBE] Color-transfer scan failed: {exc}")
        return {"type": "local", "audio_tracks": tracks, "is_hdr": source_hdr}
    except (Exception,) as e:
        _print(f"[PROBE] Local probe failed: {e}")
        return None


def _audio_sort_key(entry):
    """Sort key for YouTube audio formats: English or original language first,
    then highest average bitrate (lower key = better)."""
    lang = (entry.get("language") or "").lower()
    is_en = 0 if lang.startswith("en") else 1
    lp = entry.get("language_preference")
    original = 0 if (lp is not None and lp < 0) else 1
    abr = entry.get("abr")
    if not isinstance(abr, (int, float)):
        abr = 0
    return is_en, original, -abr


def _probe_url_audio_tracks(url):
    global _last_yt_error
    _last_yt_error = None
    try:
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }
        # noinspection PyTypeChecker
        with _ytdlp_stderr_filter, yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None or not info:
                return None

            video_url = info.get("url")
            title = info.get("title", "Unknown")
            duration = info.get("duration", 0) or 0
            is_live = info.get("is_live", False) or False

            audio_tracks = []
            formats = info.get("formats") or []

            video_headers = {}
            source_hdr = False
            best_video = None
            for fmt in formats:
                if fmt.get("vcodec", "none") != "none" and fmt.get("height", 0):
                    if format_is_hdr(fmt):
                        source_hdr = True
                    if best_video is None:  # noqa: SIM114 (split so PyCharm narrows best_video)
                        best_video = fmt
                    elif (fmt.get("height", 0) or 0) > (best_video.get("height", 0) or 0):
                        best_video = fmt
            if best_video is not None:
                video_headers = best_video.get("http_headers", {})
                if not video_url:
                    video_url = best_video.get("url")

            video_candidates = rank_video_format_candidates(formats)
            if video_candidates:
                _print("[PROBE] Video candidates: " + ", ".join(
                    f"{c.tier}:{c.vcodec.split('.')[0]} {(f'{c.fps:g}' if c.fps else '?')}fps"
                    for c in video_candidates))

            idx = 0
            for fmt in formats:
                acodec = fmt.get("acodec", "none")
                vcodec = fmt.get("vcodec", "none")
                if acodec != "none" and (vcodec == "none" or vcodec is None):
                    ext = fmt.get("ext", "")
                    abr = fmt.get("abr")
                    format_note = fmt.get("format_note", "")
                    fmt_url = fmt.get("url", "")
                    if not fmt_url:
                        continue
                    parts = [f"Format {fmt.get('format_id', idx)}:"]
                    if ext:
                        parts.append(ext)
                    if abr:
                        parts.append(f"{int(abr)}kbps")  # type: ignore
                    elif format_note:
                        parts.append(format_note)
                    if acodec and acodec != "none":
                        parts.append(f"({acodec})")
                    if fmt.get("language"):
                        parts.append(f"[{fmt.get('language') or ''}]".upper())
                    display = " ".join(parts)

                    audio_tracks.append({
                        "index": idx,
                        "format_id": fmt.get("format_id", ""),
                        "title": display,
                        "codec": acodec,
                        "channels": "",
                        "language": fmt.get("language", ""),
                        "abr": fmt.get("abr"),
                        "language_preference": fmt.get("language_preference"),
                        "url": fmt_url,
                        "http_headers": fmt.get("http_headers", {}),
                    })
                    idx += 1

            if audio_tracks:
                audio_tracks.sort(key=_audio_sort_key)
                _print(f"[PROBE] Default audio: {audio_tracks[0]['title']}")

            return {
                "type": "url",
                "video_url": video_url or "",
                "title": title,
                "duration": duration,
                "is_live": is_live,
                "video_headers": video_headers,
                "video_candidates": video_candidates,
                "audio_tracks": audio_tracks,
                "is_hdr": source_hdr,
            }
    except (Exception,) as e:
        _print(f"[PROBE] URL probe failed: {e}")
        _last_yt_error = _classify_yt_error(e)
        return None
