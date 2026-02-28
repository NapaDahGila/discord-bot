import os
import json
import discord
from discord.ext import commands
from groq import Groq
from flask import Flask
from threading import Thread

# Ambil dari env Render (wajib!)
TOKEN = os.getenv("TOKEN")          # DISCORD_TOKEN kalau mau ganti nama
GROQ_KEY = os.getenv("GROQ_KEY")

if not TOKEN:
    print("TOKEN tidak ditemukan di environment variables!")
    exit(1)

if not GROQ_KEY:
    print("GROQ_KEY tidak ditemukan!")
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

# ===== FLASK KEEP-ALIVE untuk Render free tier (biar gak sleep) =====
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot Groq + Discord online! 🚀"

def run_flask():
    port = int(os.getenv("PORT", 10000))  # Render kasih PORT via env, fallback 10000
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ==========================

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

    # Tambah pesan user
    user_memory[user_id].append({
        "role": "user",
        "content": message
    })

    # Batasi memory (10 pesan terakhir)
    user_memory[user_id] = user_memory[user_id][-10:]

    async with ctx.typing():
        try:
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": "You are a helpful Discord bot."}
                ] + user_memory[user_id]
            )

            reply = response.choices[0].message.content or "AI gak ngasih respon 😅"

            # Simpan jawaban AI
            user_memory[user_id].append({
                "role": "assistant",
                "content": reply
            })

            save_memory()  # permanen

            if len(reply) > 2000:
                reply = reply[:1990] + "..."

            await ctx.send(reply)

        except Exception as e:
            print("ERROR:", e)
            await ctx.send("AI error 😅")

# Jalankan Flask di thread terpisah + bot
if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    bot.run(TOKEN)
