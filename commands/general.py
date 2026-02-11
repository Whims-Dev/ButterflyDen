import discord
from discord import app_commands

def setup(tree: app_commands.CommandTree):
    @tree.command(name="ping", description="Check if the bot is alive")
    @app_commands.allowed_contexts(dms=True,guilds=True,private_channels=True)
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
            )
        )