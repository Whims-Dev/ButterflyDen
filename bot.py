from cProfile import label
import os
import discord
import subprocess
import time
import asyncio
from pathlib import Path
from discord import app_commands
from dotenv import load_dotenv
from jobs.job import Job
import tempfile
import xml.etree.ElementTree as ET
import json
from collections import defaultdict

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
DOWNLOAD_LOCK = asyncio.Lock()

RBXMK_PATH = os.getenv("RBXMK_PATH", "rbxmk")
MAX_RBX_NODES = 800
MAX_RBX_DEPTH = 40

BASE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = BASE_DIR / "tools"

RBXMK_PATH = TOOLS_DIR / "rbxmk"
RBX_HIERARCHY_LUA = TOOLS_DIR / "rbx_hierarchy.lua"

CLASS_ICONS = {
    "Model": "📦",
    "Folder": "📁",

    "Part": "🧱",
    "MeshPart": "🔷",
    "UnionOperation": "🔶",

    "Attachment": "📍",
    "WeldConstraint": "🔗",
    "Motor6D": "🦴",

    "Script": "📜",
    "LocalScript": "📜",
    "ModuleScript": "📘",

    "Humanoid": "🧍",
    "Animator": "🎞️",
}
DEFAULT_ICON = "▫️"

@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}")

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

def _get_name_from_item(item: ET.Element):
    props = item.find("Properties")
    if props is None:
        return None

    for s in props.findall("string"):
        if s.attrib.get("name") == "Name":
            return s.text or ""

    for s in props.findall("ProtectedString"):
        if s.attrib.get("name") == "Name":
            return s.text or ""

    return None

def build_rbxmx_hierarchy_text(rbxmx_path: Path):
    tree = ET.parse(rbxmx_path)
    root = tree.getroot()

    lines = []
    node_count = 0
    truncated = False

    def walk(item: ET.Element, depth: int):
        nonlocal node_count, truncated

        if truncated:
            return

        if node_count >= MAX_RBX_NODES or depth >= MAX_RBX_DEPTH:
            truncated = True
            return

        node_count += 1

        class_name = item.attrib.get("class", "Instance")
        name = _get_name_from_item(item) or class_name

        indent = "  " * depth
        icon = CLASS_ICONS.get(class_name, DEFAULT_ICON)
        label = f"{name} ({class_name})" if name != class_name else class_name
        lines.append(f"{indent}{icon} {label}")

        for child in item.findall("Item"):
            walk(child, depth + 1)

    top_items = root.findall("Item")
    if not top_items:
        raise RuntimeError("RBXMX has no <Item> entries")

    for it in top_items:
        walk(it, 0)

    if truncated:
        lines.append("")
        lines.append(f"[truncated: limit reached (nodes={MAX_RBX_NODES}, depth={MAX_RBX_DEPTH})]")

    return "\n".join(lines), node_count, truncated

@tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong")

@tree.command(
    name="about",
    description="About Butterfly's Den and its creator"
)
async def about(interaction: discord.Interaction):
    await interaction.response.send_message(
        content=(
            "**🦋 Butterfly's Den**\n\n"
            "Created by **Whims-Dev**\n\n"
            "🔗 GitHub: https://github.com/Whims-Dev\n"
            "🌐 Portfolio & Availability: https://discord.gg/Zfd8Jx4Eq2\n"
        ),
        ephemeral=True
    )

@tree.command(
    name="mp3",
    description="Download audio as MP3"
)
@app_commands.describe(url="Media URL (YouTube, Twitter video, etc.)")
async def mp3(interaction: discord.Interaction, url: str):
    if DOWNLOAD_LOCK.locked():
        await interaction.response.send_message(
            "⏳ Busy. Try again shortly.",
            ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)

    async with DOWNLOAD_LOCK:
        job = Job()
        start = time.monotonic()

        try:
            result = run_ytdlp([
                "-x",
                "--audio-format", "mp3",
                "--no-playlist",
                "--paths", str(job.path),
                "-o", "%(title)s_%(id)s.%(ext)s",
                url
            ])

            if result.returncode != 0:
                await interaction.edit_original_response(
                    content="❌ Failed to extract audio."
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
async def download(interaction: discord.Interaction, url: str):
    if DOWNLOAD_LOCK.locked():
        await interaction.response.send_message(
            "⏳ Busy. Try again shortly.",
            ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)

    async with DOWNLOAD_LOCK:
        job = Job()
        start = time.monotonic()

        try:
            result = run_ytdlp([
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

                result = run_ytdlp([
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

                    if compress_video(videos[0], compressed):
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

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.attachments:
        return

    for att in message.attachments:
        name = (att.filename or "").lower()
        if not (name.endswith(".rbxm") or name.endswith(".rbxmx")):
            continue

        async with message.channel.typing():
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                src = tmp / att.filename
                await att.save(src)

                try:
                    if src.suffix.lower() == ".rbxmx":
                        text, node_count, truncated = build_rbxmx_hierarchy_text(src)
                    else:  # .rbxm
                        proc = subprocess.run(
                            [
                                str(RBXMK_PATH),
                                "run",
                                str(RBX_HIERARCHY_LUA),
                                "--",
                                str(src),
                            ],
                            capture_output=True,
                            text=True,
                        )

                        stdout = proc.stdout.strip()
                        stderr = proc.stderr.strip()

                        if proc.returncode != 0 or not stdout:
                            reason = "Unknown error"

                            if "path not accessible" in stderr:
                                reason = (
                                    "rbxmk could not access the file path.\n"
                                    "This usually means the file is outside its working directory."
                                )
                            elif stderr:
                                reason = stderr.strip().splitlines()[-1]
                            elif not stdout:
                                reason = "No output was produced by rbxmk."

                            await message.reply(
                                "❌ **Failed to read RBXM file**\n"
                                f"{reason}\n\n"
                                "💡 *Tip:* This is usually an internal tool issue, not your file."
                            )
                            continue

                        nodes = json.loads(stdout)
                        children = defaultdict(list)
                        root_nodes = []

                        for n in nodes:
                            if n["parent"] == -1:
                                root_nodes.append(n)
                            else:
                                children[n["parent"]].append(n)

                        lines = []
                        count = 0
                        truncated = False

                        def walk(node, depth):
                            nonlocal count, truncated
                            if count >= MAX_RBX_NODES or depth >= MAX_RBX_DEPTH:
                                truncated = True
                                return

                            count += 1
                            indent = "  " * depth
                            cls = node["class"]
                            name = node["name"]
                            icon = CLASS_ICONS.get(cls, DEFAULT_ICON)
                            label = f"{name} ({cls})" if name != cls else cls
                            lines.append(f"{indent}{icon} {label}")

                            for child in children.get(node["id"], []):
                                walk(child, depth + 1)

                        for node in root_nodes:
                            walk(node, 0)

                        if truncated:
                            lines.append("")
                            lines.append(f"[truncated: limit reached (nodes={MAX_RBX_NODES}, depth={MAX_RBX_DEPTH})]")

                        text = "\n".join(lines)
                        node_count = count

                except Exception as e:
                    await message.reply(f"❌ Failed to parse model:\n```{str(e)[:1800]}```")
                    continue

                title = f"🧩 RBX Hierarchy: {att.filename}"
                header = f"Instances: {node_count}" + (" (truncated)" if truncated else "")
                block = f"```text\n{text}\n```"

                if len(block) <= 3800:
                    embed = discord.Embed(
                        title=title,
                        description=f"{header}\n\n{block}"
                    )
                    await message.reply(embed=embed)
                else:
                    out = tmp / (Path(att.filename).stem + ".hierarchy.txt")
                    out.write_text(text, encoding="utf-8")

                    embed = discord.Embed(
                        title=title,
                        description=f"{header}\n\nToo large to display inline, attached as a file."
                    )
                    await message.reply(embed=embed, file=discord.File(out))

client.run(os.getenv("DISCORD_TOKEN"))