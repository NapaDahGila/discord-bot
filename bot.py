import os
import json
import sqlite3
import discord
from discord.ext import commands
from groq import Groq

TOKEN = os.getenv("TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")

client = Groq(api_key=GROQ_KEY)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ===== MEMORY SYSTEM (SQLite) =====

DB_FILE = "memory.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS memory (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   TEXT NOT NULL,
            role      TEXT NOT NULL,
            content   TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def load_memory(user_id: str, limit: int = 15) -> list:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Ambil N pesan terakhir, terus dibalik biar urutannya bener
    c.execute("""
        SELECT role, content FROM (
            SELECT id, role, content FROM memory
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        ) ORDER BY id ASC
    """, (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": r, "content": c} for r, c in rows]

def save_message(user_id: str, role: str, content: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO memory (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content)
    )
    # Hapus pesan lama kalau udah lebih dari 15 (biar DB ga bengkak)
    c.execute("""
        DELETE FROM memory WHERE user_id = ? AND id NOT IN (
            SELECT id FROM memory WHERE user_id = ? ORDER BY id DESC LIMIT 15
        )
    """, (user_id, user_id))
    conn.commit()
    conn.close()

init_db()

# ==========================


@bot.event
async def on_ready():
    print(f"Bot online sebagai {bot.user}")


@bot.command()
async def ping(ctx):
    await ctx.send("Pong 🏓")

def is_creator_question(text):
    keywords = ["dibuat siapa", "desain siapa", "siapa yang buat"]
    return any(k in text for k in keywords)


@bot.command()
async def chat(ctx, *, message):

    if not GROQ_KEY:
        await ctx.send("API key Groq belum diset.")
        return

    user_id = str(ctx.author.id)

    save_message(user_id, "user", message)
    history = load_memory(user_id)

    async with ctx.typing():
        try:
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are Enki, a smart and slightly sarcastic AI assistant. "
                            "You are a Discord bot. "
                            "If anyone asks who designed or created you, "
                            "answer: 'Bot ini didesain sama Ren Lumireign.' "
                            "Do not mention OpenAI or any model."
                        )
                    }
                ] + history
            )

            reply = response.choices[0].message.content or "AI gak ngasih respon 😅"

            save_message(user_id, "assistant", reply)

            if len(reply) > 2000:
                reply = reply[:1990] + "..."

            await ctx.send(reply)

        except Exception as e:
            print("ERROR:", e)
            await ctx.send("AI error 😅")

@bot.event
async def on_message(message):

    if message.author == bot.user:
        return

    await bot.process_commands(message)

    text = message.content.lower()

    if is_creator_question(text):
        await message.channel.send("Bot ini di desain oleh Ren Lumireign")
        return

    if message.channel.name != "enki":
        return

    user_id = str(message.author.id)
    nickname = message.author.display_name

    save_message(user_id, "user", message.content)
    history = load_memory(user_id)

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You are Enki, a smart and slightly sarcastic AI assistant. "
                        f"Your user's name is {nickname}. "
                        "You are a Discord bot. "
                        "If anyone asks who designed or created you, "
                        "answer: 'Bot ini didesain sama Ren Lumireign.' "
                        "Do not mention OpenAI or any model."
                    )
                }
            ] + history
        )

        reply = response.choices[0].message.content

        save_message(user_id, "assistant", reply)

        await message.channel.send(reply)

    except Exception as e:
        print("ERROR:", e)
        await message.channel.send("AI error 😅")

@bot.command()
async def debug(ctx, *, question: str = None):
    """Debug file Python yang di-upload sebagai attachment"""
    if not ctx.message.attachments:
        await ctx.send("Upload file Python dulu 🔥")
        return

    file = ctx.message.attachments[0]

    if not file.filename.endswith(".py"):
        await ctx.send("Cuma bisa debug file `.py`")
        return

    # Batasi ukuran file (max 50KB)
    if file.size > 50_000:
        await ctx.send("File terlalu besar (max 50KB)")
        return

    try:
        content = await file.read()
        code = content.decode("utf-8")
    except Exception as e:
        await ctx.send(f"Gagal baca file: {e}")
        return

    # Kalau user nanya sesuatu spesifik, sertakan pertanyaannya
    user_prompt = f"Debug this Python code:\n\n```python\n{code}\n```"
    if question:
        user_prompt += f"\n\nFokus ke masalah ini: {question}"

    async with ctx.typing():
        try:
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an expert Python debugger. "
                            "Identify bugs clearly, explain why it's a bug, "
                            "and provide the fixed code with short explanation."
                        )
                    },
                    {
                        "role": "user",
                        "content": user_prompt
                    }
                ]
            )

            reply = response.choices[0].message.content
            if len(reply) > 2000:
                reply = reply[:1990] + "..."

            await ctx.reply(reply)  # reply ke message user, bukan kirim baru

        except Exception as e:
            await ctx.reply(f"AI error: {e}")

if not TOKEN:
    print("ERROR: TOKEN tidak ditemukan!")
else:
    bot.run(TOKEN)
