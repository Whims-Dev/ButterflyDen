import time

import discord
from discord import app_commands

from jobs.job import Job
from shared import DOWNLOAD_LOCK
from media import *

def setup(tree: app_commands.CommandTree):
    @tree.command(
        name="mp3",
        description="Download audio as MP3"
    )
    @app_commands.describe(url="Media URL (YouTube, Twitter video, etc.)")
    @app_commands.allowed_contexts(dms=True,guilds=True,private_channels=True)
    async def mp3(interaction: discord.Interaction, url: str):
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
                result = await run_ytdlp([
                    "--no-playlist",
                    "--paths", str(job.path),
                    "-o", "%(title)s_%(id)s.%(ext)s",
                    url
                ])

                if result.returncode != 0:
                    if "Unsupported URL" in result.stderr:
                        await interaction.edit_original_response(
                            content="❌ Unsupported site."
                        )
                        return

                    is_image_capable_site = any(
                        domain in url for domain in ("twitter.com", "x.com")
                    )

                    if not is_image_capable_site:
                        await interaction.edit_original_response(
                            content="❌ Failed to extract media."
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
                            content="❌ Failed to extract media."
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
    @app_commands.describe(url="Media URL (YouTube, Twitter, Instagram, etc.)")
    @app_commands.allowed_contexts(dms=True,guilds=True,private_channels=True)
    async def download(interaction: discord.Interaction, url: str):
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
                result = await run_ytdlp([
                    "--no-playlist",
                    "--paths", str(job.path),
                    "-o", "%(title)s_%(id)s.%(ext)s",
                    url
                ])

                if result.returncode != 0:
                    if "Unsupported URL" in result.stderr:
                        await interaction.edit_original_response(
                            content="❌ Unsupported site."
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
                            content="❌ Failed to extract media."
                        )
                        return

                files = [f for f in job.path.iterdir() if f.is_file()]
                videos = [f for f in files if f.suffix.lower() in {".mp4", ".mkv", ".webm"}]

                if not files:
                    await interaction.edit_original_response(
                        content="❌ No media found."
                    )
                    return

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