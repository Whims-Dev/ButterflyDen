import subprocess
from pathlib import Path

def run_ytdlp(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["yt-dlp", *args],
        capture_output=True,
        text=True
    )

def compress_video(input_path: Path, output_path: Path) -> bool:
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i", str(input_path),
            "-map_metadata", "-1",
            "-c:v", "libx264",
            "-preset", "slow",
            "-crf", "28",
            "-c:a", "aac",
            "-b:a", "128k",
            str(output_path)
        ],
        capture_output=True,
        text=True
    )
    return result.returncode == 0

