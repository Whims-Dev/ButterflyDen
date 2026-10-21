import asyncio
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

REDDIT_LINK_RE = re.compile(r'<a href="([^"]+)">\[link\]</a>')

async def resolve_reddit_media(url: str) -> tuple[str, str] | None:
    """Find the media behind a Reddit link without logging in.

    Reddit blocks anonymous requests to its pages and API (so yt-dlp fails), but
    a post's RSS feed, i.redd.it and v.redd.it are still reachable.
    Returns ("image" | "video" | "external", url), or None if nothing was found.
    """
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(headers=BROWSER_HEADERS, timeout=timeout) as session:
            host = urlsplit(url).netloc.lower()
            if host != "i.redd.it" and host != "v.redd.it":
                # share links (/r/sub/s/...) and redd.it short links redirect to the post
                async with session.get(url, allow_redirects=True) as response:
                    parts = urlsplit(str(response.url))
                if "/comments/" not in parts.path:
                    return None
                feed_url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/.rss", "", ""))

                text = None
                # anonymous requests get a small rate limit, so retry a few times
                for attempt in range(4):
                    async with session.get(feed_url) as response:
                        if response.status == 200:
                            text = await response.text()
                            break
                    await asyncio.sleep(6)
                if text is None:
                    return None

                entry = ET.fromstring(text).find("{http://www.w3.org/2005/Atom}entry")
                content = entry.findtext("{http://www.w3.org/2005/Atom}content") if entry is not None else None
                match = REDDIT_LINK_RE.search(content or "")
                if not match:
                    return None
                url = match.group(1)

        media = urlsplit(url)
        if media.netloc.lower() == "i.redd.it":
            return "image", url
        if media.netloc.lower() == "v.redd.it":
            video_id = media.path.strip("/").split("/")[0]
            return "video", f"https://v.redd.it/{video_id}/HLSPlaylist.m3u8"
        if is_reddit(url):
            return None  # gallery or text post
        return "external", url
    except (aiohttp.ClientError, asyncio.TimeoutError, ET.ParseError):
        return None

async def download_file(url: str, destination: Path) -> bool:
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(headers=BROWSER_HEADERS, timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return False
                destination.write_bytes(await response.read())
        return True
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False

async def run_ytdlp(args: list[str]) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(
        subprocess.run,
        [
            "yt-dlp",
            "--extractor-args", "youtube:player_client=android",
            *args
        ],
        capture_output=True,
        text=True
    )

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
