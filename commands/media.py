import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

import discord
from discord import app_commands

from jobs.job import Job
from shared import DOWNLOAD_LOCK
from media import *

MAX_DURATION_SECONDS = 20 * 60
MAX_GIF_SECONDS = 30
MAX_GIF_LENGTH = 15
MAX_COMPRESS_INPUT_MB = 250
DEFAULT_COMPRESS_RATIO = 0.5

MAX_SLIDESHOW_SECONDS = 90
SECONDS_PER_IMAGE = 3
VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm"}

def channel_allows_nsfw(interaction: discord.Interaction) -> bool:
    """DMs and age-restricted channels (threads inherit from their parent)."""
    if interaction.guild is None:
        return True
    channel = interaction.channel
    return channel is not None and getattr(channel, "is_nsfw", lambda: False)()

async def send_booru_post(interaction: discord.Interaction, job: Job, post_url: str, start: float) -> None:
    """Download one e621 / e926 post with gallery-dl and send it."""
    resolved = await resolve_booru_post(post_url)
    if resolved is None:
        await interaction.edit_original_response(content="❌ Couldn't open that link.")
        return

    site, post_url = resolved
    if site == "e621" and not channel_allows_nsfw(interaction):
        await interaction.edit_original_response(
            content="🔞 e621 posts can only be downloaded in age-restricted channels or DMs."
        )
        return

    if not await run_gallerydl(post_url, job.path):
        await interaction.edit_original_response(
            content="❌ Couldn't get that post. It may have been deleted or blocked."
        )
        return

    files = [f for f in job.path.rglob("*") if f.is_file()]
    if not files:
        await interaction.edit_original_response(content="❌ No media found.")
        return

    media_file = files[0]
    size_mb = media_file.stat().st_size / (1024 * 1024)
    if size_mb > 25:
        compressed = job.path / "compressed.mp4"
        if (
            media_file.suffix.lower() in VIDEO_SUFFIXES
            and await compress_video(media_file, compressed)
            and compressed.stat().st_size / (1024 * 1024) <= 25
        ):
            media_file = compressed
        else:
            await interaction.edit_original_response(
                content=f"⚠️ Media too large ({size_mb:.1f} MB)."
            )
            return

    elapsed = max(round(time.monotonic() - start, 2), 0.01)
    await interaction.edit_original_response(
        content=f"`Download time: {elapsed}s`",
        attachments=[discord.File(media_file)]
    )

REDDIT_FAILED = (
    "❌ Couldn't get media from that Reddit post. Galleries and text posts aren't "
    "supported, and Reddit may be rate limiting me, so try again in a minute."
)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".opus"}

async def send_tiktok_gallery(interaction: discord.Interaction, job: Job, url: str, start: float) -> bool:
    """Fetch a TikTok photo post with gallery-dl and send its images plus audio."""
    for item in job.path.iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    if not await run_gallerydl(url, job.path):
        return False

    files = [f for f in job.path.rglob("*") if f.is_file()]
    images = sorted(f for f in files if f.suffix.lower() in IMAGE_SUFFIXES)
    audio = [f for f in files if f.suffix.lower() in AUDIO_SUFFIXES]
    if not images:
        return False

    # plain names so the ffmpeg concat list needs no escaping
    simple_images = []
    for i, image in enumerate(images, start=1):
        renamed = image.with_name(f"slide_{i:03d}{image.suffix.lower()}")
        image.rename(renamed)
        simple_images.append(renamed)
    images = simple_images

    audio_length = await get_media_duration(audio[0]) if audio else None
    total_seconds = min(
        audio_length if audio_length else len(images) * SECONDS_PER_IMAGE,
        MAX_SLIDESHOW_SECONDS
    )
    slideshow = job.path / "slideshow.mp4"
    if (
        await make_slideshow(images, audio[0] if audio else None, slideshow, total_seconds)
        and slideshow.stat().st_size / (1024 * 1024) <= 25
    ):
        elapsed = max(round(time.monotonic() - start, 2), 0.01)
        await interaction.edit_original_response(
            content=f"`Download time: {elapsed}s`",
            attachments=[discord.File(slideshow)]
        )
        return True

    attachments = images[:9] + audio[:1]
    total_mb = sum(f.stat().st_size for f in attachments) / (1024 * 1024)
    if total_mb > 25:
        attachments = images[:10]
        total_mb = sum(f.stat().st_size for f in attachments) / (1024 * 1024)
        if total_mb > 25:
            await interaction.edit_original_response(
                content=f"⚠️ Media too large ({total_mb:.1f} MB)."
            )
            return True

    elapsed = max(round(time.monotonic() - start, 2), 0.01)
    await interaction.edit_original_response(
        content=f"`Download time: {elapsed}s`",
        attachments=[discord.File(f) for f in attachments]
    )
    return True

def setup(tree: app_commands.CommandTree):
    @tree.command(
        name="mp3",
        description="Download audio as MP3"
    )
    @app_commands.describe(url="Media URL (YouTube, TikTok, Twitter video, etc.)")
    @app_commands.allowed_contexts(dms=True,guilds=True,private_channels=True)
    async def mp3(interaction: discord.Interaction, url: str):
        if DOWNLOAD_LOCK.locked():
            await interaction.response.send_message(
                content="⏳ Busy. Try again shortly.",
                ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        if is_tiktok(url):
            url = await resolve_tiktok_url(url)

        if is_reddit(url):
            target = await resolve_reddit_media(url)
            if target is None:
                await interaction.edit_original_response(content=REDDIT_FAILED)
                return
            if target[0] == "image":
                await interaction.edit_original_response(
                    content="❌ That Reddit post is an image, not a video."
                )
                return
            url = target[1]

        async with DOWNLOAD_LOCK:
            job = Job()
            start = time.monotonic()

            try:
                result = await run_ytdlp([
                    "--no-playlist",
                    "-x",
                    "--audio-format", "mp3",
                    "--audio-quality", "0",
                    "--paths", str(job.path),
                    "-o", "%(title)s_%(id)s.%(ext)s",
                    url
                ])

                if result.returncode != 0:
                    first_error = result.stderr
                    if "Unsupported URL" in first_error:
                        await interaction.edit_original_response(
                            content=ytdlp_failure_message(url, first_error)
                        )
                        return

                    is_image_capable_site = any(
                        domain in url for domain in ("twitter.com", "x.com")
                    )

                    if not is_image_capable_site:
                        await interaction.edit_original_response(
                            content=ytdlp_failure_message(url, first_error)
                        )
                        return

                    result = await run_ytdlp([
                        "--no-playlist",
                        "--paths", str(job.path),
                        "--skip-download",
                        "--write-all-thumbnails",
                        "--convert-thumbnails", "jpg",
                        "-o", "%(title)s_%(id)s.%(ext)s",
                        url
                    ])

                    if result.returncode != 0:
                        await interaction.edit_original_response(
                            content=ytdlp_failure_message(url, first_error)
                        )
                        return

                files = list(job.path.glob("*.mp3"))
                if not files:
                    await interaction.edit_original_response(
                        content="❌ No MP3 was produced."
                    )
                    return

                mp3_file = files[0]
                size_mb = mp3_file.stat().st_size / (1024 * 1024)

                if size_mb > 25:
                    await interaction.edit_original_response(
                        content=f"⚠️ MP3 too large ({size_mb:.1f} MB)."
                    )
                    return

                elapsed = round(time.monotonic() - start, 2)

                await interaction.edit_original_response(
                    content=f"`Download time: {elapsed}s`",
                    attachments=[discord.File(mp3_file)]
                )

            finally:
                job.cleanup()

    @tree.command(
        name="download",
        description="Download media (video or images)"
    )
    @app_commands.describe(url="Media URL (YouTube, TikTok, Twitter, Reddit, e621, etc.)")
    @app_commands.allowed_contexts(dms=True,guilds=True,private_channels=True)
    async def download(interaction: discord.Interaction, url: str):
        booru = parse_booru_post(url)
        if booru and booru[0] == "e621" and not channel_allows_nsfw(interaction):
            await interaction.response.send_message(
                content="🔞 e621 posts can only be downloaded in age-restricted channels or DMs. "
                        "e926 (the safe version) works anywhere.",
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

        if is_tiktok(url):
            url = await resolve_tiktok_url(url)

        async with DOWNLOAD_LOCK:
            job = Job()
            start = time.monotonic()

            try:
                if (
                    is_tiktok(url)
                    and "/photo/" in url
                    and await send_tiktok_gallery(interaction, job, url, start)
                ):
                    return

                if booru:
                    await send_booru_post(interaction, job, booru[1], start)
                    return

                if is_reddit(url):
                    target = await resolve_reddit_media(url)
                    if target is None:
                        await interaction.edit_original_response(content=REDDIT_FAILED)
                        return

                    kind, url = target
                    if kind == "image":
                        image = job.path / (Path(urlsplit(url).path).name or "image.jpg")
                        if not await download_file(url, image):
                            await interaction.edit_original_response(
                                content="❌ Failed to download the image."
                            )
                            return

                        size_mb = image.stat().st_size / (1024 * 1024)
                        if size_mb > 25:
                            await interaction.edit_original_response(
                                content=f"⚠️ Image too large ({size_mb:.1f} MB)."
                            )
                            return

                        elapsed = max(round(time.monotonic() - start, 2), 0.01)
                        await interaction.edit_original_response(
                            content=f"`Download time: {elapsed}s`",
                            attachments=[discord.File(image)]
                        )
                        return

                duration = await get_duration(url)

                if duration is not None and duration > MAX_DURATION_SECONDS:
                    minutes = duration / 60
                    await interaction.edit_original_response(
                        content=f"⚠️ Video too long ({minutes:.1f} min, max {MAX_DURATION_SECONDS // 60} min)."
                    )
                    return

                result = await run_ytdlp([
                    "--no-playlist",
                    *VIDEO_FORMAT_ARGS,
                    "--paths", str(job.path),
                    "-o", "%(title)s_%(id)s.%(ext)s",
                    url
                ])

                if result.returncode != 0:
                    first_error = result.stderr
                    if "Unsupported URL" in first_error:
                        await interaction.edit_original_response(
                            content=ytdlp_failure_message(url, first_error)
                        )
                        return

                    if is_tiktok(url) and await send_tiktok_gallery(interaction, job, url, start):
                        return

                    result = await run_ytdlp([
                        "--no-playlist",
                        "--paths", str(job.path),
                        "--skip-download",
                        "--write-all-thumbnails",
                        "--convert-thumbnails", "jpg",
                        "-o", "%(title)s_%(id)s.%(ext)s",
                        url
                    ])

                    if result.returncode != 0:
                        await interaction.edit_original_response(
                            content=ytdlp_failure_message(url, first_error)
                        )
                        return

                files = [f for f in job.path.iterdir() if f.is_file()]
                videos = [f for f in files if f.suffix.lower() in {".mp4", ".mkv", ".webm"}]

                # yt-dlp only returns the audio for TikTok slideshows
                if is_tiktok(url) and not videos and await send_tiktok_gallery(interaction, job, url, start):
                    return

                if not files:
                    await interaction.edit_original_response(
                        content="❌ No media found."
                    )
                    return

                if (
                    is_twitter(url)
                    and len(files) == 1
                    and len(videos) == 1
                    and (duration is None or duration <= MAX_GIF_SECONDS)
                    and not await has_audio(videos[0])
                ):
                    gif = job.path / "animation.gif"
                    if (
                        await video_to_gif(videos[0], gif)
                        and gif.stat().st_size / (1024 * 1024) <= 25
                    ):
                        elapsed = max(round(time.monotonic() - start, 2), 0.01)
                        await interaction.edit_original_response(
                            content=f"`Download time: {elapsed}s`",
                            attachments=[discord.File(gif)]
                        )
                        return

                # HEVC / AV1 (common in Medal clips) show as audio-only on many devices
                for video in list(videos):
                    if await needs_h264(video):
                        converted = video.with_name(f"{video.stem}_h264.mp4")
                        if await convert_to_h264(video, converted):
                            video.unlink()
                            files = [converted if f == video else f for f in files]
                            videos = [converted if v == video else v for v in videos]

                total_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
                if total_mb > 25:
                    if len(videos) == 1:
                        compressed = job.path / "compressed.mp4"

                        if await compress_video(videos[0], compressed):
                            new_size = compressed.stat().st_size / (1024 * 1024)

                            if new_size <= 25:
                                elapsed = max(round(time.monotonic() - start, 2), 0.01)
                                await interaction.edit_original_response(
                                    content=f"`Download time: {elapsed}s`",
                                    attachments=[discord.File(compressed)]
                                )
                                return

                    await interaction.edit_original_response(
                        content=f"⚠️ Media too large ({total_mb:.1f} MB), even after compression."
                    )
                    return

                elapsed = max(round(time.monotonic() - start, 2), 0.01)
                discord_files = [discord.File(f) for f in files[:10]]

                await interaction.edit_original_response(
                    content=f"`Download time: {elapsed}s`",
                    attachments=discord_files
                )

            finally:
                job.cleanup()

    @tree.command(
        name="gif",
        description="Turn a video into a GIF"
    )
    @app_commands.describe(
        url="Media URL (YouTube, TikTok, Twitter, etc.)",
        start="Where to start, e.g. 30 or 1:30 (default: beginning)",
        length=f"Seconds to capture, 1-{MAX_GIF_LENGTH} (default: 8)"
    )
    @app_commands.allowed_contexts(dms=True,guilds=True,private_channels=True)
    async def gif(
        interaction: discord.Interaction,
        url: str,
        start: str = "0",
        length: app_commands.Range[int, 1, MAX_GIF_LENGTH] = 8
    ):
        start_seconds = parse_timestamp(start)
        if start_seconds is None:
            await interaction.response.send_message(
                content="❌ Invalid start time. Use seconds (`30`) or `1:30`.",
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

        if is_tiktok(url):
            url = await resolve_tiktok_url(url)

        if is_reddit(url):
            target = await resolve_reddit_media(url)
            if target is None:
                await interaction.edit_original_response(content=REDDIT_FAILED)
                return
            if target[0] == "image":
                await interaction.edit_original_response(
                    content="❌ That Reddit post is an image, not a video."
                )
                return
            url = target[1]

        async with DOWNLOAD_LOCK:
            job = Job()
            start_time = time.monotonic()

            try:
                # yt-dlp's section download returns a truncated clip for Reddit's HLS
                # streams, so those are downloaded whole and trimmed by ffmpeg instead
                trim_with_ffmpeg = url.endswith("HLSPlaylist.m3u8")
                if trim_with_ffmpeg:
                    duration = await get_duration(url)
                    if duration is not None and duration > MAX_DURATION_SECONDS:
                        await interaction.edit_original_response(
                            content=f"⚠️ Video too long ({duration / 60:.1f} min, max {MAX_DURATION_SECONDS // 60} min)."
                        )
                        return

                end_seconds = start_seconds + length
                result = await run_ytdlp([
                    "--no-playlist",
                    *VIDEO_FORMAT_ARGS,
                    *(
                        []
                        if trim_with_ffmpeg
                        else [
                            "--download-sections", f"*{start_seconds}-{end_seconds}",
                            "--force-keyframes-at-cuts",
                        ]
                    ),
                    "--paths", str(job.path),
                    "-o", "%(id)s.%(ext)s",
                    url
                ])

                if result.returncode != 0:
                    await interaction.edit_original_response(
                        content=ytdlp_failure_message(url, result.stderr)
                    )
                    return

                videos = [
                    f for f in job.path.iterdir()
                    if f.is_file() and f.suffix.lower() in {".mp4", ".mkv", ".webm"}
                ]
                if not videos:
                    await interaction.edit_original_response(
                        content="❌ No video found."
                    )
                    return

                output = job.path / "animation.gif"
                for width in (480, 320, 240):
                    if (
                        await video_to_gif(
                            videos[0], output, width,
                            *((start_seconds, length) if trim_with_ffmpeg else ())
                        )
                        and output.stat().st_size / (1024 * 1024) <= 25
                    ):
                        elapsed = max(round(time.monotonic() - start_time, 2), 0.01)
                        await interaction.edit_original_response(
                            content=f"`Download time: {elapsed}s`",
                            attachments=[discord.File(output)]
                        )
                        return

                await interaction.edit_original_response(
                    content="⚠️ GIF too large, even when shrunk. Try a shorter length."
                )

            finally:
                job.cleanup()

    @tree.command(
        name="compress",
        description="Shrink a video so it fits under a size limit"
    )
    @app_commands.describe(
        video="The video file to compress",
        target_mb="Size to stay under in MB, 0.5-25 (default: half the video's size, max 25)"
    )
    @app_commands.allowed_contexts(dms=True,guilds=True,private_channels=True)
    async def compress(
        interaction: discord.Interaction,
        video: discord.Attachment,
        target_mb: app_commands.Range[float, 0.5, 25.0] | None = None
    ):
        content_type = video.content_type or ""
        if not content_type.startswith("video/") and Path(video.filename).suffix.lower() not in VIDEO_SUFFIXES | {".mov", ".m4v", ".avi"}:
            await interaction.response.send_message(
                content="❌ That doesn't look like a video file.",
                ephemeral=True
            )
            return

        if video.size > MAX_COMPRESS_INPUT_MB * 1024 * 1024:
            await interaction.response.send_message(
                content=f"❌ That file is too big for me to handle ({video.size / (1024 * 1024):.0f} MB, max {MAX_COMPRESS_INPUT_MB} MB).",
                ephemeral=True
            )
            return

        size_mb = video.size / (1024 * 1024)
        if target_mb is None:
            target_mb = round(min(25, max(0.5, size_mb * DEFAULT_COMPRESS_RATIO)), 1)

        if size_mb <= target_mb:
            await interaction.response.send_message(
                content=f"✅ That's already under {target_mb:g} MB ({size_mb:.1f} MB), nothing to compress.",
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

        async with DOWNLOAD_LOCK:
            job = Job()
            start = time.monotonic()

            try:
                source = job.path / f"input{Path(video.filename).suffix.lower() or '.mp4'}"
                await video.save(source)

                duration = await get_media_duration(source)
                if duration is None or duration <= 0:
                    await interaction.edit_original_response(
                        content="❌ Couldn't read that video. The file may be damaged."
                    )
                    return

                if duration > MAX_DURATION_SECONDS:
                    await interaction.edit_original_response(
                        content=f"⚠️ Video too long ({duration / 60:.1f} min, max {MAX_DURATION_SECONDS // 60} min)."
                    )
                    return

                output = job.path / f"{Path(video.filename).stem}_compressed.mp4"
                if not await compress_to_size(source, output, target_mb, duration, await has_audio(source)):
                    await interaction.edit_original_response(
                        content=f"⚠️ Couldn't get that under {target_mb:g} MB without ruining it. Try a larger target or a shorter clip."
                    )
                    return

                elapsed = max(round(time.monotonic() - start, 2), 0.01)
                before = video.size / (1024 * 1024)
                after = output.stat().st_size / (1024 * 1024)
                await interaction.edit_original_response(
                    content=f"`{before:.1f} MB → {after:.1f} MB in {elapsed}s`",
                    attachments=[discord.File(output)]
                )

            finally:
                job.cleanup()
