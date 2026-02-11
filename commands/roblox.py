import re
import tempfile
from pathlib import Path

import aiohttp
import discord
from discord import app_commands

def setup(tree: app_commands.CommandTree):
    @tree.command(
        name="emitmodule",
        description="Reply with the latest release rbxm file for VFX Forge's emit module"
    )
    @app_commands.allowed_contexts(dms=True, guilds=True, private_channels=True)
    async def emitmodule(interaction: discord.Interaction):
        await interaction.response.defer()

        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.github.com/repos/zilibobi/forge-vfx/releases/latest",
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "ButterflyDen",
                },
            ) as resp:
                release = await resp.json()

            for asset in release.get("assets", []):
                if not asset["name"].endswith(".rbxm"):
                    continue

                async with session.get(asset["browser_download_url"]) as file_resp:
                    with tempfile.NamedTemporaryFile(delete=False) as tmp:
                        tmp.write(await file_resp.read())
                        tmp_path = Path(tmp.name)

                try:
                    await interaction.followup.send(
                        file=discord.File(tmp_path, filename=asset["name"])
                    )
                finally:
                    tmp_path.unlink(missing_ok=True)

                return

        await interaction.followup.send(
            "I couldn’t find an `.rbxm` file in the latest release"
        )

    @tree.command(
        name="rbxlibrary",
        description="Convert a Roblox asset ID into a Creator Store link"
    )
    @app_commands.describe(asset="Roblox asset ID or rbxassetid:// format")
    async def rbxlibrary(interaction: discord.Interaction, asset: str):
        s = asset.strip()

        match = re.search(r"rbxassetid://(\d+)", s, re.IGNORECASE)
        if not match:
            match = re.search(r"(\d{5,})", s)

        if not match:
            await interaction.response.send_message(
                "I couldn't find a valid Roblox asset ID in that 🐾",
                ephemeral=True
            )
            return

        asset_id = match.group(1)
        url = f"https://create.roblox.com/store/asset/{asset_id}"

        await interaction.response.send_message(url)