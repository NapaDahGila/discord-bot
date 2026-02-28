import os
import json
import discord
from discord.ext import commands
from groq import Groq

# Ambil dari env Fly.io (wajib!)
TOKEN = os.getenv("TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")

if not TOKEN or not GROQ_KEY:
    print("TOKEN atau GROQ_KEY tidak ditemukan di secrets!")
    exit(1)

client = Groq(api_key=GROQ_KEY)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ===== MEMORY SYSTEM =====
MEMORY_FILE = "memory.json"

if os.path.exists(MEMORY_FILE):
    with open(MEMORY_FILE, "r") as f:
        user_memory = json.load(f)
else:
    user_memory = {}

def save_memory():
    with open(MEMORY_FILE, "w") as f:
        json.dump(user_memory, f, indent=4)

@bot.event
async def on_ready():
    print(f"Bot online sebagai {bot.user}")

@bot.command()
async def ping(ctx):
    await ctx.send("Pong 🏓")

@bot.command()
async def chat(ctx, *, message):
    if not GROQ_KEY:
        await ctx.send("API key Groq belum diset.")
        return

    user_id = str(ctx.author.id)

    if user_id not in user_memory:
        user_memory[user_id] = []

    user_memory[user_id].append({"role": "user", "content": message})
    user_memory[user_id] = user_memory[user_id][-10:]  # batasi 10 pesan

    async with ctx.typing():
        try:
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "system", "content": "You are a helpful Discord bot."}] + user_memory[user_id]
            )

            reply = response.choices[0].message.content or "AI gak ngasih respon 😅"

            user_memory[user_id].append({"role": "assistant", "content": reply})
            save_memory()

            if len(reply) > 2000:
                reply = reply[:1990] + "..."

            await ctx.send(reply)

        except Exception as e:
            print("ERROR:", e)
            await ctx.send("AI error 😅")

bot.run(TOKEN)
