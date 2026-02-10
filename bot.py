# ——— Standard library
import os

# ——— Third party
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}")

from commands import general, media, roblox
from listeners import attachments

for module in (general, media, roblox):
    module.setup(tree)

for module in (attachments,):
    module.setup(client)

client.run(os.getenv("DISCORD_TOKEN"))