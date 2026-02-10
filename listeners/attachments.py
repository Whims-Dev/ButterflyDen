import json
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

import discord

from roblox.hierarchy import (
    build_rbxmx_hierarchy_text,
    RBXMK_PATH,
    RBX_HIERARCHY_LUA,
    MAX_RBX_NODES,
    MAX_RBX_DEPTH,
    CLASS_ICONS,
    DEFAULT_ICON
)

def setup(client: discord.Client):
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
                            text, node_count, truncated = build_rbxmx_hierarchy_text(
                                src)
                        else:  # .rbxm
                            proc = subprocess.run(
                                [
                                    str(RBXMK_PATH),
                                    "run",
                                    str(RBX_HIERARCHY_LUA),
                                    "--",
                                    str(src),
                                ],
                                cwd=tmp,
                                capture_output=True,
                                text=True,
                            )

                            stdout = proc.stdout.strip()
                            stderr = proc.stderr.strip()

                            if proc.returncode != 0 or not stdout:
                                err = stderr.strip() or "No error output"

                                await message.reply(
                                    "❌ **Failed to read RBXM file**\n\n"
                                    "**rbxmk error:**\n"
                                    f"```lua\n{err[:1800]}\n```"
                                )
                                continue

                            try:
                                nodes = json.loads(stdout)
                            except json.JSONDecodeError as e:
                                preview_out = stdout[:1500].strip() or "<empty>"
                                preview_err = stderr[:1500].strip() or "<empty>"

                                await message.reply(
                                    "❌ **RBXM parser returned invalid output**\n\n"
                                    "**stdout:**\n"
                                    f"```{preview_out}```\n"
                                    "**stderr:**\n"
                                    f"```{preview_err}```"
                                )
                                continue

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
                                lines.append(
                                    f"[truncated: limit reached (nodes={MAX_RBX_NODES}, depth={MAX_RBX_DEPTH})]")

                            text = "\n".join(lines)
                            node_count = count

                    except Exception as e:
                        await message.reply(f"❌ Failed to parse model:\n```{str(e)[:1800]}```")
                        continue

                    title = f"🧩 RBX Hierarchy: {att.filename}"
                    header = f"Instances: {node_count}" + \
                        (" (truncated)" if truncated else "")
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
