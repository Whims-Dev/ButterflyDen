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
from graphviz import Digraph

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
DOWNLOAD_LOCK = asyncio.Lock()

RBXMK_PATH = os.getenv("RBXMK_PATH", "rbxmk")
MAX_RBX_NODES = 300

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

def convert_rbxm_to_rbxmx(src: Path, dst: Path):
    script = f"""
local v = fs.read("{src.as_posix()}", "rbxm")
fs.write("{dst.as_posix()}", v, "rbxmx")
"""
    with tempfile.NamedTemporaryFile("w", suffix=".lua", delete=False) as f:
        f.write(script)
        script_path = f.name

    try:
        subprocess.run(
            [RBXMK_PATH, "run", script_path],
            check=True,
            capture_output=True
        )
    finally:
        os.remove(script_path)


def parse_rbxmx_tree(path: Path):
    tree = ET.parse(path)
    root = tree.getroot()

    nodes = []
    edges = []
    node_id = 0

    def walk(item, parent=None):
        nonlocal node_id
        if node_id >= MAX_RBX_NODES:
            return

        node_id += 1
        my_id = node_id

        class_name = item.attrib.get("class", "Instance")
        name = class_name

        props = item.find("Properties")
        if props is not None:
            for s in props.findall("string"):
                if s.attrib.get("name") == "Name":
                    name = s.text or class_name

        nodes.append((my_id, name, class_name))
        if parent:
            edges.append((parent, my_id))

        for child in item.findall("Item"):
            walk(child, my_id)

    for top in root.findall("Item"):
        walk(top)

    return nodes, edges


def render_tree_svg(nodes, edges, out_path: Path):
    dot = Digraph("RBX", format="svg")
    dot.attr(rankdir="LR", fontsize="10")

    for i, name, cls in nodes:
        dot.node(f"n{i}", f"{name}\n<{cls}>")

    for a, b in edges:
        dot.edge(f"n{a}", f"n{b}")

    dot.render(out_path, cleanup=True)

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
        name = att.filename.lower()
        if not (name.endswith(".rbxm") or name.endswith(".rbxmx")):
            continue

        await message.channel.typing()

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / att.filename
            await att.save(src)

            rbxmx = src
            if src.suffix == ".rbxm":
                rbxmx = tmp / (src.stem + ".rbxmx")
                try:
                    convert_rbxm_to_rbxmx(src, rbxmx)
                except Exception as e:
                    await message.reply(f"❌ RBXM conversion failed:\n```{e}```")
                    return

            try:
                nodes, edges = parse_rbxmx_tree(rbxmx)
            except Exception as e:
                await message.reply(f"❌ Failed to parse model:\n```{e}```")
                return

            svg_path = tmp / "tree"
            render_tree_svg(nodes, edges, svg_path)

            file = discord.File(f"{svg_path}.svg", filename="rbx_tree.svg")

            embed = discord.Embed(
                title="🧩 Roblox Model Preview",
                description=(
                    f"**Instances:** {len(nodes)}\n"
                    f"**Links:** {len(edges)}\n"
                    + ("⚠️ Truncated" if len(nodes) >= MAX_RBX_NODES else "")
                ),
                color=0x9b6cff
            )
            embed.set_image(url="attachment://rbx_tree.svg")

            await message.reply(embed=embed, file=file)

client.run(os.getenv("DISCORD_TOKEN"))