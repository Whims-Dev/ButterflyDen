import asyncio
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import discord
from discord import app_commands

from jobs.job import Job
from shared import DOWNLOAD_LOCK
from media import (
    get_duration,
    get_media_duration,
    is_tiktok,
    resolve_tiktok_url,
    run_ytdlp,
    ytdlp_failure_message,
)

log = logging.getLogger(__name__)

STEMS_SCRIPT = Path(__file__).resolve().parent.parent / "stems.py"

# How long a job may run before it is killed. Discord only lets the bot answer an
# interaction for 15 minutes, so keep this comfortably under that.
JOB_TIMEOUT_SECONDS = int(os.getenv("STEM_TIMEOUT", "720"))

# How much slower this machine is than the fast PC the cost table below was measured on.
# 1 = about as fast as that PC. Raise it on slow hardware (a Raspberry Pi) so the length
# limits shrink to what can finish in time. Measure with `python stems.py <song> <dir> <mode>`.
HARDWARE_SLOWDOWN = float(os.getenv("STEM_SLOWDOWN", "6"))

# Seconds of work per second of audio, measured on a 16-core desktop CPU (no GPU).
COST_PER_AUDIO_SECOND = {
    ("vocals", "fast"): 0.7,
    ("vocals", "best"): 2.3,
    ("stems4", "fast"): 0.55,
    ("stems4", "best"): 1.9,
    ("stems6", "fast"): 0.55,
    ("stems6", "best"): 0.55,
    ("drumkit", "fast"): 3.7,
    ("drumkit", "best"): 5.1,
}
STARTUP_SECONDS = 30  # loading models, encoding the results

MAX_UPLOAD_MB = 50
MAX_MESSAGE_MB = 24
MAX_FILES_PER_MESSAGE = 10
AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac", ".mp4", ".webm", ".mkv", ".mov"}

MODE_SHORT_NAMES = {
    "vocals": "vocals + instrumental split",
    "stems4": "4-stem split",
    "stems6": "6-stem split",
    "drumkit": "drum kit split",
}

STEM_LABELS = {
    "vocals": "vocals",
    "instrumental": "instrumental",
    "drums": "drums",
    "bass": "bass",
    "other": "other",
    "guitar": "guitar",
    "piano": "piano",
    "kick": "kick",
    "snare": "snare",
    "toms": "toms",
    "hh": "hi-hat",
    "ride": "ride",
    "crash": "crash",
}


def separation_installed() -> bool:
    return STEMS_SCRIPT.exists() and importlib.util.find_spec("audio_separator") is not None


def longest_allowed_seconds(mode: str, quality: str) -> float:
    cost = COST_PER_AUDIO_SECOND[(mode, quality)] * HARDWARE_SLOWDOWN
    return max((JOB_TIMEOUT_SECONDS * 0.9 - STARTUP_SECONDS) / cost, 0)


def estimate_seconds(duration: float, mode: str, quality: str) -> float:
    return STARTUP_SECONDS + duration * COST_PER_AUDIO_SECOND[(mode, quality)] * HARDWARE_SLOWDOWN


def clock(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def safe_name(text: str) -> str:
    cleaned = re.sub(r"[^\w\- ]+", "", text).strip().replace(" ", "_")
    return cleaned[:40] or "song"


def run_separation(source: Path, out_dir: Path, mode: str, quality: str) -> tuple[dict[str, Path] | None, str]:
    """Run stems.py in its own process. Returns (stems, error message)."""
    command = [sys.executable, str(STEMS_SCRIPT), str(source), str(out_dir), mode, quality]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=JOB_TIMEOUT_SECONDS,
            cwd=str(STEMS_SCRIPT.parent),
            # be polite to the bot and the rest of the machine while it works
            preexec_fn=(lambda: os.nice(10)) if os.name == "posix" else None,
        )
    except subprocess.TimeoutExpired:
        log.warning("stems: %s/%s timed out after %ss", mode, quality, JOB_TIMEOUT_SECONDS)
        return None, "timeout"

    for line in result.stdout.splitlines():
        if line.startswith("Separated in"):
            log.info("stems: %s", line)

    if result.returncode != 0:
        log.warning("stems: failed (exit %s): %s", result.returncode, (result.stderr or result.stdout)[-1500:])
        return None, "failed"

    for line in reversed(result.stdout.splitlines()):
        if line.startswith("RESULT "):
            return {name: Path(path) for name, path in json.loads(line[len("RESULT "):]).items()}, ""

    log.warning("stems: no RESULT line in output: %s", result.stdout[-800:])
    return None, "failed"


def encode_mp3(source: Path, destination: Path, bitrate_kbps: int) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(source), "-map_metadata", "-1",
         "-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k", str(destination)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def pack_into_messages(files: list[Path]) -> list[list[Path]]:
    """Group files into messages that stay under Discord's size and attachment limits."""
    messages, current, current_size = [], [], 0
    for file in files:
        size = file.stat().st_size
        if current and (len(current) >= MAX_FILES_PER_MESSAGE or current_size + size > MAX_MESSAGE_MB * 1024 * 1024):
            messages.append(current)
            current, current_size = [], 0
        current.append(file)
        current_size += size
    if current:
        messages.append(current)
    return messages


async def deliver(interaction: discord.Interaction, first: bool, content: str, files: list[Path]) -> None:
    """Send a message, falling back to the channel if the interaction has expired."""
    attachments = [discord.File(file) for file in files]
    try:
        if first:
            await interaction.edit_original_response(content=content, attachments=attachments)
        else:
            await interaction.followup.send(content=content, files=attachments)
    except discord.HTTPException:
        # long jobs can outlive the interaction's 15 minute window
        attachments = [discord.File(file) for file in files]
        message = f"{interaction.user.mention} {content}"
        if interaction.channel is not None:
            await interaction.channel.send(content=message, files=attachments)
        else:
            await interaction.user.send(content=content, files=attachments)


def setup(tree: app_commands.CommandTree):
    @tree.command(
        name="split",
        description="Split a song into vocals, instrumental, or separate instruments"
    )
    @app_commands.describe(
        mode="What to split the song into",
        quality="Fast is quicker; best uses stronger models and takes much longer",
        audio="The song as a file",
        url="...or a link to it (YouTube, SoundCloud, etc.)"
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Vocals + instrumental", value="vocals"),
            app_commands.Choice(name="4 stems: vocals, drums, bass, other", value="stems4"),
            app_commands.Choice(name="6 stems: adds guitar and piano", value="stems6"),
            app_commands.Choice(name="Drum kit: kick, snare, hi-hat... plus vocals, bass, other", value="drumkit"),
        ],
        quality=[
            app_commands.Choice(name="Fast", value="fast"),
            app_commands.Choice(name="Best (much slower)", value="best"),
        ],
    )
    @app_commands.allowed_contexts(dms=True, guilds=True, private_channels=True)
    async def split(
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
        audio: discord.Attachment | None = None,
        url: str | None = None,
        quality: app_commands.Choice[str] | None = None
    ):
        mode_value = mode.value
        quality_value = quality.value if quality else "fast"

        if not separation_installed():
            await interaction.response.send_message(
                content="❌ Song splitting isn't set up on this bot yet (the `audio-separator` package is missing).",
                ephemeral=True
            )
            return

        if (audio is None) == (url is None):
            await interaction.response.send_message(
                content="❌ Give me either a file (`audio`) or a link (`url`), not both and not neither.",
                ephemeral=True
            )
            return

        if audio is not None:
            if audio.size > MAX_UPLOAD_MB * 1024 * 1024:
                await interaction.response.send_message(
                    content=f"❌ That file is too big (max {MAX_UPLOAD_MB} MB).",
                    ephemeral=True
                )
                return
            kind = audio.content_type or ""
            if not (kind.startswith("audio/") or kind.startswith("video/") or Path(audio.filename).suffix.lower() in AUDIO_SUFFIXES):
                await interaction.response.send_message(
                    content=f"❌ `{audio.filename}` doesn't look like an audio file.",
                    ephemeral=True
                )
                return

        if DOWNLOAD_LOCK.locked():
            await interaction.response.send_message(
                content="⏳ Busy. Try again shortly.",
                ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        if url is not None and is_tiktok(url):
            url = await resolve_tiktok_url(url)

        async with DOWNLOAD_LOCK:
            job = Job()
            start = time.monotonic()

            try:
                source_dir = job.path / "input"
                source_dir.mkdir()

                if audio is not None:
                    source = source_dir / f"song{Path(audio.filename).suffix.lower() or '.mp3'}"
                    await audio.save(source)
                    title = Path(audio.filename).stem
                else:
                    pre_duration = await get_duration(url)
                    limit = longest_allowed_seconds(mode_value, quality_value)
                    if pre_duration is not None and pre_duration > limit:
                        await interaction.edit_original_response(
                            content=f"⚠️ That's {clock(pre_duration)} long, but on this machine the {MODE_SHORT_NAMES[mode_value]} "
                                    f"in {quality_value} quality tops out around {clock(limit)}."
                        )
                        return

                    result = await run_ytdlp([
                        "--no-playlist",
                        "-x", "--audio-format", "wav",
                        "--paths", str(source_dir),
                        "-o", "%(title)s.%(ext)s",
                        url
                    ])
                    downloaded = [f for f in source_dir.iterdir() if f.is_file()]
                    if result.returncode != 0 or not downloaded:
                        await interaction.edit_original_response(
                            content=ytdlp_failure_message(url, result.stderr)
                        )
                        return
                    source = downloaded[0]
                    title = source.stem

                duration = await get_media_duration(source)
                if duration is None or duration <= 0:
                    await interaction.edit_original_response(
                        content="❌ Couldn't read that audio. The file may be damaged."
                    )
                    return

                limit = longest_allowed_seconds(mode_value, quality_value)
                if duration > limit:
                    await interaction.edit_original_response(
                        content=f"⚠️ That's {clock(duration)} long, but on this machine the {MODE_SHORT_NAMES[mode_value]} "
                                f"in {quality_value} quality tops out around {clock(limit)}. "
                                f"Try a shorter clip" + (", or fast quality." if quality_value == "best" else ".")
                    )
                    return

                await interaction.edit_original_response(
                    content=f"🎛️ Doing a {MODE_SHORT_NAMES[mode_value]} on **{title[:60]}** ({quality_value} quality). "
                            f"This can take up to about {clock(estimate_seconds(duration, mode_value, quality_value))}."
                )

                stems, problem = await asyncio.to_thread(
                    run_separation, source, job.path / "separated", mode_value, quality_value
                )
                if stems is None:
                    if problem == "timeout":
                        content = "⚠️ That took too long and was stopped. Try a shorter clip or fast quality."
                    else:
                        content = "❌ Something went wrong while splitting that. Check the bot's log for details."
                    await interaction.edit_original_response(content=content)
                    return

                # encode to MP3; step the bitrate down if a single stem would be too big to send
                base = safe_name(title)
                mp3_files: list[Path] = []
                bitrate = 192
                for stem, path in stems.items():
                    target = path.with_name(f"{base}_{STEM_LABELS.get(stem, stem)}.mp3")
                    for bitrate in (192, 128, 96):
                        if not await asyncio.to_thread(encode_mp3, path, target, bitrate):
                            target = None
                            break
                        if target.stat().st_size <= MAX_MESSAGE_MB * 1024 * 1024:
                            break
                    if target is None or target.stat().st_size > MAX_MESSAGE_MB * 1024 * 1024:
                        await interaction.edit_original_response(
                            content="⚠️ The results came out too large to send."
                        )
                        return
                    mp3_files.append(target)

                elapsed = max(round(time.monotonic() - start), 1)
                batches = pack_into_messages(mp3_files)
                for number, batch in enumerate(batches):
                    note = f"`{len(mp3_files)} files in {clock(elapsed)}`" if number == 0 else f"`(part {number + 1}/{len(batches)})`"
                    if len(batches) > 1 and number == 0:
                        note += f" `(part 1/{len(batches)})`"
                    await deliver(interaction, number == 0, note, batch)

            finally:
                job.cleanup()
