import asyncio
import subprocess
from pathlib import Path

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