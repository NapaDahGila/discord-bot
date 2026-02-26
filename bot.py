import os
import discord
from discord.ext import commands

TOKEN = os.getenv("TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Bot online sebagai {bot.user}")

@bot.command()
async def ping(ctx):
    await ctx.send("Pong 🏓")

bot.run(TOKEN)    