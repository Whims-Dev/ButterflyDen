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

def slide_indicator_filter(count: int, per_image: float) -> str:
    """Row of dots at the bottom; the dot for the current slide is bigger and brighter."""
    step = min(28, 640 // count)
    size = max(step // 2, 6)
    active = size + 6
    y = 1215
    x0 = (720 - (count - 1) * step) // 2

    parts = []
    for i in range(count):
        cx = x0 + i * step
        parts.append(
            f"drawbox=x={cx - size // 2}:y={y - size // 2}:w={size}:h={size}:color=white@0.45:t=fill"
        )
    for i in range(count):
        cx = x0 + i * step
        start, end = i * per_image, (i + 1) * per_image
        parts.append(
            f"drawbox=x={cx - active // 2}:y={y - active // 2}:w={active}:h={active}:color=white@1:t=fill:"
            f"enable='gte(t,{start - 0.01:.3f})*lt(t,{end - 0.01:.3f})'"
        )
    return ",".join(parts)

async def make_slideshow(images: list[Path], audio: Path | None, output_path: Path, total_seconds: float) -> bool:
    """Show each image for an equal share of total_seconds, with audio underneath."""
    per_image = total_seconds / len(images)

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
    video_filter = (
        "scale=720:1280:force_original_aspect_ratio=decrease,"
        "pad=720:1280:(ow-iw)/2:(oh-ih)/2"
    )
    if len(images) > 1:
        video_filter += "," + slide_indicator_filter(len(images), per_image)
    video_filter += ",format=yuv420p"

    cmd += [
        "-vf", video_filter,
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
