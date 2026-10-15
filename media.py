import asyncio
import subprocess
import sys
from pathlib import Path

def is_tiktok(url: str) -> bool:
    return "tiktok.com" in url.lower()

def is_twitter(url: str) -> bool:
    return any(d in url.lower() for d in ("twitter.com", "x.com"))

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

async def video_to_gif(input_path: Path, output_path: Path, width: int = 480) -> bool:
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-y",
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
