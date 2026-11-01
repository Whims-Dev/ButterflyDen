import asyncio
import html
import logging
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from pathlib import Path

def is_tiktok(url: str) -> bool:
    return "tiktok.com" in url.lower()

def is_twitter(url: str) -> bool:
    return any(d in url.lower() for d in ("twitter.com", "x.com"))

log = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    )
}

async def resolve_tiktok_url(url: str) -> str:
    """Follow TikTok share links (tiktok.com/t/..., vm.tiktok.com/...) to the real post URL."""
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(headers=BROWSER_HEADERS, timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as response:
                final = str(response.url)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return url

    parts = urlsplit(final)
    if "tiktok.com" not in parts.netloc or parts.path in ("", "/"):
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

def is_reddit(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return host.endswith("reddit.com") or host.endswith("redd.it")

BOORU_POST_RE = re.compile(
    r"^https?://(?:www\.)?(e621|e926)\.net/(?:posts/(\d+)|p/(\w+))",
    re.IGNORECASE
)

def parse_booru_post(url: str) -> tuple[str, str] | None:
    """Return (site, post URL) for an e621 / e926 link.

    Short links (/p/<code>) are returned as-is; resolve_booru_post follows them.
    """
    match = BOORU_POST_RE.match(url.strip())
    if not match:
        return None
    site = match.group(1).lower()
    if match.group(2):
        return site, f"https://{site}.net/posts/{match.group(2)}"
    return site, f"https://{site}.net/p/{match.group(3)}"

async def resolve_booru_post(url: str) -> tuple[str, str] | None:
    """Turn a short /p/<code> link into the real /posts/<id> link."""
    if "/p/" not in url:
        return parse_booru_post(url)
    try:
        _, final_url, _ = await curl(url, output=Path(os.devnull))
    except (OSError, subprocess.SubprocessError):
        return None
    resolved = parse_booru_post(final_url)
    return resolved if resolved and "/posts/" in resolved[1] else None

REDDIT_LINK_RE = re.compile(r'<a href="([^"]+)">\[link\]</a>')

async def curl(url: str, output: Path | None = None) -> tuple[int, str, bytes]:
    """Fetch a URL with curl and return (HTTP status, final URL, body).

    Reddit answers curl where it blocks Python's HTTP client from the Pi.
    With `output` set the body is written to that file instead of returned.
    """
    marker = b"<<curl>>"
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "curl", "-sL", "--max-time", "30",
            "-A", BROWSER_HEADERS["User-Agent"],
            *(["-o", str(output)] if output else []),
            "-w", marker.decode() + "%{http_code} %{url_effective}",
            url
        ],
        capture_output=True
    )
    body, _, tail = result.stdout.rpartition(marker)
    status, _, final_url = tail.decode(errors="replace").partition(" ")
    return (int(status) if status.isdigit() else 0), final_url.strip(), body

REDDIT_EMBED_TYPE_RE = re.compile(r'"embed":\{[^}]*?"type":"(\w+)"')
REDDIT_EMBED_MEDIA_RES = {
    "image": re.compile(r'https://i\.redd\.it/[\w.-]+'),
    "video": re.compile(r'https://v\.redd\.it/\w+'),
}

async def reddit_embed_link(post_path: str) -> str | None:
    """Media link from the post's embed page, the public page Reddit offers for embedding posts."""
    embed_url = f"https://embed.reddit.com{post_path}/"
    body = None
    for attempt in range(2):
        status, _, body = await curl(embed_url)
        if status == 200:
            break
        log.warning("Reddit: embed %s returned HTTP %s (attempt %s)", embed_url, status, attempt + 1)
        body = None
        if status != 429:
            return None
        await asyncio.sleep(8)
    if body is None:
        return None

    text = html.unescape(body.decode("utf-8", errors="replace"))
    kind = REDDIT_EMBED_TYPE_RE.search(text)
    media_re = REDDIT_EMBED_MEDIA_RES.get(kind.group(1)) if kind else None
    match = media_re.search(text) if media_re else None
    if not match:
        log.warning("Reddit: no image or video in the embed page %s (type %s)", embed_url, kind.group(1) if kind else None)
        return None
    return match.group(0)

async def reddit_feed_link(post_path: str) -> str | None:
    """Media link from the post's RSS feed. Reddit has announced it ends feeds on 2026-11-13."""
    feed_url = f"https://www.reddit.com{post_path}/.rss"
    text = None
    # anonymous requests get a small rate limit; only 429 is worth retrying
    for attempt in range(3):
        status, _, body = await curl(feed_url)
        if status == 200:
            text = body.decode("utf-8", errors="replace")
            break
        log.warning("Reddit: feed %s returned HTTP %s (attempt %s)", feed_url, status, attempt + 1)
        if status != 429:
            return None
        await asyncio.sleep(8)
    if text is None:
        return None

    entry = ET.fromstring(text).find("{http://www.w3.org/2005/Atom}entry")
    content = entry.findtext("{http://www.w3.org/2005/Atom}content") if entry is not None else None
    match = REDDIT_LINK_RE.search(content or "")
    if not match:
        log.warning("Reddit: no media link in the feed for %s", feed_url)
        return None
    return match.group(1)

async def resolve_reddit_media(url: str) -> tuple[str, str] | None:
    """Find the media behind a Reddit link without logging in.

    Reddit blocks anonymous requests to its pages and API (so yt-dlp fails), but
    a post's embed page and RSS feed, i.redd.it and v.redd.it are still reachable.
    Returns ("image" | "video" | "external", url), or None if nothing was found.
    """
    try:
        host = urlsplit(url).netloc.lower()
        if host != "i.redd.it" and host != "v.redd.it":
            # share links (/r/sub/s/...) and redd.it short links redirect to the post
            status, final_url, _ = await curl(url, output=Path(os.devnull))
            parts = urlsplit(final_url)
            if "/comments/" not in parts.path:
                log.warning("Reddit: %s went to %s (HTTP %s), which is not a post", url, final_url, status)
                return None
            post_path = parts.path.rstrip("/")
            media_link = await reddit_embed_link(post_path)
            if media_link is None:
                media_link = await reddit_feed_link(post_path)
            if media_link is None:
                return None
            url = media_link

        media = urlsplit(url)
        if media.netloc.lower() == "i.redd.it":
            return "image", url
        if media.netloc.lower() == "v.redd.it":
            video_id = media.path.strip("/").split("/")[0]
            return "video", f"https://v.redd.it/{video_id}/HLSPlaylist.m3u8"
        if is_reddit(url):
            log.warning("Reddit: %s is a gallery or text post", url)
            return None
        return "external", url
    except (OSError, subprocess.SubprocessError, ET.ParseError) as error:
        log.warning("Reddit: lookup failed for %s: %r", url, error)
        return None

async def download_file(url: str, destination: Path) -> bool:
    try:
        status, _, _ = await curl(url, output=destination)
        return status == 200 and destination.exists()
    except (OSError, subprocess.SubprocessError):
        return False

# Closest to 720p without going over, preferring H.264 + AAC so files play on every device
# and stay small enough for Discord. Without a cap yt-dlp would pick 4K VP9 (hundreds of MB).
VIDEO_FORMAT_ARGS = [
    "-S", "res:720,vcodec:h264,acodec:aac",
    "--merge-output-format", "mp4",
]

async def run_ytdlp(args: list[str]) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(
        subprocess.run,
        [
            "yt-dlp",
            *args
        ],
        capture_output=True,
        text=True
    )

def ytdlp_failure_message(url: str, stderr: str) -> str:
    """Explain a failed yt-dlp run to the user, and log the full error for the bot owner."""
    log.warning("yt-dlp failed for %s: %s", url, stderr.strip()[-1500:])

    host = urlsplit(url).netloc.lower().removeprefix("www.") or "that link"
    text = stderr.lower()

    if "unsupported url" in text:
        if host in ("cdn.discordapp.com", "media.discordapp.net"):
            return f"❌ That's a Discord file link that isn't a video. Only videos can be downloaded from `{host}`."
        return (
            f"❌ I can't download from `{host}`. It's either not a supported site, "
            "or the link doesn't point to a video."
        )
    if "private video" in text or "this video is private" in text:
        return f"❌ That video on `{host}` is private."
    if "age" in text and ("confirm your age" in text or "age-restricted" in text or "age restricted" in text):
        return f"❌ That video on `{host}` is age-restricted, so I can't download it."
    if any(phrase in text for phrase in ("sign in", "log in", "logged-in", "logged in", "login required", "authentication is required", "use --cookies")):
        return f"❌ `{host}` wants a login for that one, so I can't download it."
    if any(phrase in text for phrase in ("geo restricted", "not available in your country", "geo-restricted")):
        return f"❌ That video on `{host}` isn't available in my region."
    if "http error 429" in text or "too many requests" in text:
        return f"❌ `{host}` is rate limiting me. Try again in a few minutes."
    if "http error 403" in text or "forbidden" in text or "blocked" in text:
        return f"❌ `{host}` blocked my request (403). The link may have expired or the site may block bots."
    if "http error 404" in text or "not found" in text:
        return f"❌ `{host}` says that doesn't exist (404). It may have been deleted, or the link has expired."
    if any(phrase in text for phrase in ("video unavailable", "video is unavailable", "has been removed", "no longer available")):
        return f"❌ That video on `{host}` is unavailable or was removed."
    if "requested format is not available" in text:
        return f"❌ `{host}` has no downloadable format for that video."

    errors = [line for line in stderr.splitlines() if line.startswith("ERROR:")]
    detail = ""
    if errors:
        short = " ".join(errors[-1].removeprefix("ERROR:").split())
        detail = chr(10) + f"`{short[:200]}`"
    return f"❌ Failed to extract media from `{host}`.{detail}"

async def get_duration(url: str) -> float | None:
    result = await run_ytdlp([
        "--no-playlist",
        "--skip-download",
        "--print", "%(duration)s",
        url
    ])

    if result.returncode != 0:
        return None

    output = result.stdout.strip()
    if not output or output == "NA":
        return None

    try:
        return float(output)
    except ValueError:
        return None

async def needs_h264(path: Path) -> bool:
    """True for videos many players can't decode: HEVC, AV1, or 10-bit / non-4:2:0 H.264."""
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,pix_fmt",
            "-of", "csv=p=0",
            str(path)
        ],
        capture_output=True,
        text=True
    )
    parts = result.stdout.strip().split(",")
    if result.returncode != 0 or len(parts) < 2:
        return False

    codec, pix_fmt = parts[0], parts[1]
    if codec in ("hevc", "av1"):
        return True
    return codec == "h264" and pix_fmt not in ("yuv420p", "yuvj420p")

async def convert_to_h264(input_path: Path, output_path: Path) -> bool:
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-y",
            "-i", str(input_path),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-map_metadata", "-1",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_path)
        ],
        capture_output=True,
        text=True
    )
    return result.returncode == 0

async def compress_video(input_path: Path, output_path: Path) -> bool:
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-y",
            "-i", str(input_path),
            "-map_metadata", "-1",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "28",
            "-c:a", "aac",
            "-b:a", "128k",
            str(output_path)
        ],
        capture_output=True,
        text=True
    )
    return result.returncode == 0

async def compress_to_size(
    input_path: Path,
    output_path: Path,
    target_mb: float,
    duration: float,
    audio: bool
) -> bool:
    """Re-encode to H.264 aiming under target_mb by choosing a video bitrate.

    Lower bitrates also get a lower resolution, since a small picture at a decent
    bitrate looks better than a large one starved of bits. Retries with less
    bitrate if the first attempt lands over the target.
    """
    audio_bps = 96_000 if audio else 0
    video_bps = (target_mb * 8 * 1024 * 1024 * 0.92) / duration - audio_bps
    if video_bps < 80_000:
        return False

    for attempt in range(3):
        if video_bps >= 3_000_000:
            height = 1080
        elif video_bps >= 1_200_000:
            height = 720
        elif video_bps >= 500_000:
            height = 480
        else:
            height = 360

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-map", "0:v:0",
            *(["-map", "0:a:0"] if audio else []),
            "-map_metadata", "-1",
            "-vf", f"scale=-2:'min({height},ih)'",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-b:v", str(int(video_bps)),
            "-maxrate", str(int(video_bps * 1.3)),
            "-bufsize", str(int(video_bps * 2)),
            "-pix_fmt", "yuv420p",
            *(["-c:a", "aac", "-b:a", str(audio_bps)] if audio else []),
            "-movflags", "+faststart",
            str(output_path)
        ]
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return False
        if output_path.stat().st_size <= target_mb * 1024 * 1024:
            return True
        video_bps *= 0.8

    return False

async def has_audio(path: Path) -> bool:
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(path)
        ],
        capture_output=True,
        text=True
    )
    return result.returncode != 0 or bool(result.stdout.strip())

async def video_to_gif(
    input_path: Path,
    output_path: Path,
    width: int = 480,
    start: float | None = None,
    length: float | None = None
) -> bool:
    trim = []
    if start is not None and length is not None:
        trim = ["-ss", f"{start}", "-t", f"{length}"]

    result = await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-y",
            *trim,
            "-i", str(input_path),
            "-vf",
            f"fps=15,scale='min({width},iw)':-1:flags=lanczos,split[a][b];"
            "[a]palettegen[p];[b][p]paletteuse",
            "-loop", "0",
            str(output_path)
        ],
        capture_output=True,
        text=True
    )
    return result.returncode == 0


def parse_timestamp(text: str) -> float | None:
    """Parse "90", "1:30" or "1:02:03" into seconds."""
    try:
        seconds = 0.0
        for part in text.strip().split(":"):
            seconds = seconds * 60 + float(part)
        return seconds if seconds >= 0 else None
    except ValueError:
        return None


async def run_gallerydl(url: str, directory: Path) -> bool:
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable, "-m", "gallery_dl",
            "--directory", str(directory),
            url
        ],
        capture_output=True,
        text=True
    )
    return result.returncode == 0


async def get_media_duration(path: Path) -> float | None:
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(path)
        ],
        capture_output=True,
        text=True
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None

INDICATOR_WIDTH = 640
INDICATOR_HEIGHT = 24
INDICATOR_FPS = 15

def slide_indicator_source(count: int, per_image: float, total_seconds: float) -> str:
    """Filter chain producing a transparent strip of round dots, one per slide.

    The current slide's dot is bigger and brighter. Drawn per pixel with geq so
    no font or image library is needed.
    """
    step = min(28, INDICATOR_WIDTH // count)
    radius = max(step / 4, 3)
    active_radius = radius + 3
    x0 = (INDICATOR_WIDTH - (count - 1) * step) / 2
    cy = INDICATOR_HEIGHT / 2

    # which slide is showing at time T; the strip runs at INDICATOR_FPS, so offset
    # by half a strip frame to keep boundaries within half a frame of the video
    current = f"clip(floor((T+{0.5 / INDICATOR_FPS:.4f})/{per_image:.4f}),0,{count - 1})"
    nearest = f"clip(round((X-{x0})/{step}),0,{count - 1})"
    is_active = f"eq({nearest},{current})"
    dist = f"hypot(X-({x0}+{nearest}*{step}),Y-{cy})"
    edge = f"if({is_active},{active_radius},{radius})"
    coverage = f"clip({edge}-{dist}+0.5,0,1)"
    brightness = f"if({is_active},1,0.45)"

    return (
        f"color=c=white:s={INDICATOR_WIDTH}x{INDICATOR_HEIGHT}:r={INDICATOR_FPS}:d={total_seconds:.3f},"
        f"format=yuva444p,"
        f"geq=lum=255:cb=128:cr=128:a='255*{coverage}*{brightness}'"
    )

async def get_image_size(path: Path) -> tuple[int, int] | None:
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            str(path)
        ],
        capture_output=True,
        text=True
    )
    try:
        width, height = result.stdout.strip().split(",")[:2]
        return int(width), int(height)
    except ValueError:
        return None

async def make_slideshow(images: list[Path], audio: Path | None, output_path: Path, total_seconds: float) -> bool:
    """Show each image for an equal share of total_seconds, with audio underneath."""
    per_image = total_seconds / len(images)

    # Landscape frame when most slides are landscape, otherwise the usual vertical one
    sizes = [await get_image_size(image) for image in images]
    landscape = sum(1 for size in sizes if size and size[0] > size[1])
    if landscape * 2 > len(images):
        frame_w, frame_h = 1280, 720
    else:
        frame_w, frame_h = 720, 1280

    # Slides often differ in size, and a mid-stream size change makes ffmpeg
    # restart its filter graph and drop slides, so fit each one to the frame first.
    fitted = []
    for image in images:
        target = image.with_name(f"fit_{image.stem}.png")
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "ffmpeg", "-y", "-i", str(image),
                "-vf",
                f"scale={frame_w}:{frame_h}:force_original_aspect_ratio=decrease,"
                f"pad={frame_w}:{frame_h}:(ow-iw)/2:(oh-ih)/2,setsar=1",
                "-frames:v", "1",
                str(target)
            ],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            return False
        fitted.append(target)
    images = fitted

    list_path = output_path.parent / "slides.txt"
    lines = []
    for image in images:
        lines.append(f"file '{image.name}'")
        lines.append(f"duration {per_image:.3f}")
    # concat demuxer ignores the last duration unless the file is repeated
    lines.append(f"file '{images[-1].name}'")
    list_path.write_text(chr(10).join(lines), encoding="utf-8")

    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path)]
    if audio is not None:
        cmd += ["-i", str(audio)]
    if len(images) > 1:
        graph = (
            f"[0:v]fps=30[bg];"
            f"{slide_indicator_source(len(images), per_image, total_seconds)}[dots];"
            f"[bg][dots]overlay=x=(W-w)/2:y={frame_h - 77}:format=auto,format=yuv420p[v]"
        )
    else:
        graph = "[0:v]format=yuv420p[v]"

    cmd += ["-filter_complex", graph, "-map", "[v]"]
    if audio is not None:
        cmd += ["-map", "1:a"]
    cmd += [
        "-r", "30",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "28",
    ]
    if audio is not None:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += ["-t", f"{total_seconds:.3f}", str(output_path)]

    result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
    return result.returncode == 0
