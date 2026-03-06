import os
import json
import io
import sqlite3
import time
import discord
import pytz
from datetime import datetime
from datetime import datetime
import pytz
from discord.ext import commands
from groq import Groq

TOKEN = os.getenv("TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")

client = Groq(api_key=GROQ_KEY)

intents = discord.Intents.default()
intents.message_content = True

START_TIME = time.time()

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
    c.execute("""
        CREATE TABLE IF NOT EXISTS prefixes (
            guild_id  TEXT PRIMARY KEY,
            prefix    TEXT NOT NULL DEFAULT '!'
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

#save prefix
def get_prefix(bot, message):
    if not message.guild:
        return "!"
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT prefix FROM prefixes WHERE guild_id = ?", (str(message.guild.id),))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "!"

def set_prefix(guild_id: str, prefix: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO prefixes (guild_id, prefix) VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET prefix = ?
    """, (guild_id, prefix, prefix))
    conn.commit()
    conn.close()


bot = commands.Bot(command_prefix=get_prefix, intents=intents)
# ==========================


@bot.event
async def on_ready():
    print(f"Bot online sebagai {bot.user}")

#buat ngeping
@bot.command()
async def ping(ctx):
    await ctx.send("Pong 🏓")

def is_creator_question(text):
    keywords = ["dibuat siapa", "desain siapa", "siapa yang buat"]
    return any(k in text for k in keywords)

#buat kalo !chat ai bakal bales
@bot.command()
async def chat(ctx, *, message):

    if not GROQ_KEY:
        await ctx.send("API key Groq belum diset.")
        return

    user_id = str(ctx.author.id)

    save_message(user_id, "user", message)
    history = load_memory(user_id)

    
    wib = pytz.timezone("Asia/Jakarta")
    sekarang = datetime.now(wib).strftime("%H:%M, %d %B %Y")


    async with ctx.typing():
        try:
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Lo adalah Enki, AI asisten yang santai, sarkas, dan natural. "
                            "Ngobrol kayak temen deket — ga kaku, ga formal. "
                            "Boleh nyindir dikit tapi tetap helpful. "
                            "Jawab pake bahasa Indonesia yang santai, boleh campur bahasa gaul. "
                            "Jangan lebay, jangan terlalu panjang kalau ga perlu. "
                            "Kalau ditanya siapa yang bikin lo, jawab: 'Gw dibuat sama Ren Lumireign.' "
                            "Jangan sebut OpenAI atau model apapun.")
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

#buat khusus channel namanya "enki" auto reply
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

    wib = pytz.timezone("Asia/Jakarta")
    sekarang = datetime.now(wib).strftime("%H:%M, %d %B %Y")

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Lo adalah Enki, AI asisten yang santai, sarkas, dan natural. "
                        "Ngobrol kayak temen deket — ga kaku, ga formal. "
                        "Boleh nyindir dikit tapi tetap helpful. "
                        "Jawab pake bahasa Indonesia yang santai, boleh campur bahasa gaul. "
                        "Jangan lebay, jangan terlalu panjang kalau ga perlu. "
                        "Kalau ditanya siapa yang bikin lo, jawab: 'Gw dibuat sama Ren Lumireign.' "
                        "Jangan sebut OpenAI atau model apapun.")
                }
            ] + history
        )

        reply = response.choices[0].message.content

        save_message(user_id, "assistant", reply)

        await message.channel.send(reply)

    except Exception as e:
        print("ERROR:", e)
        await message.channel.send("AI error 😅")

#buat benerin codingan
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
                        ) # yang bagian ini bisa di ganti, "content" 
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
            else:
                import io
                file_output = io.BytesIO(reply.encode("utf-8"))
                await ctx.reply("Hasil debug terlalu panjang, nih filenya 📄",file=discord.File(file_output, filename="debug_result.txt")
    )

            await ctx.reply(reply)  # reply ke message user, bukan kirim baru

        except Exception as e:
            await ctx.reply(f"AI error: {e}")

#buat ngeroasting codingan
@bot.command()
async def roast(ctx):
    """Roast code Python yang di-upload"""
    if not ctx.message.attachments:
        await ctx.send("Upload file Python dulu biar gw hajar 😈")
        return

    file = ctx.message.attachments[0]

    if not file.filename.endswith(".py"):
        await ctx.send("Cuma bisa roast file `.py`")
        return

    if file.size > 50_000:
        await ctx.send("File terlalu besar (max 50KB)")
        return

    try:
        content = await file.read()
        code = content.decode("utf-8")
    except Exception as e:
        await ctx.send(f"Gagal baca file: {e}")
        return

    async with ctx.typing():
        try:
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a savage but funny code roaster. "
                            "Roast this code brutally but keep it humorous. "
                            "Point out bad practices, ugly code, and amateur mistakes "
                            "in a funny way. Be mean but still educational."
                            "balas pakai bahasa indonesia, ga harus sopan."
                        )
                    },
                    {
                        "role": "user",
                        "content": f"Roast this code:\n\n```python\n{code}\n```"
                    }
                ]
            )

            reply = response.choices[0].message.content

            if len(reply) <= 2000:
                await ctx.reply(reply)
            else:
                file_output = io.BytesIO(reply.encode("utf-8"))
                await ctx.reply(
                    "Roastannya panjang banget, nih filenya 🔥",
                    file=discord.File(file_output, filename="roast_result.txt")
                )

        except Exception as e:
            await ctx.reply(f"AI error: {e}")

#buat ngereview hasil codingan
@bot.command()
async def review(ctx, *, question: str = None):
    """Review code Python yang di-upload"""
    if not ctx.message.attachments:
        await ctx.send("Upload file Python dulu 📎")
        return

    file = ctx.message.attachments[0]

    if not file.filename.endswith(".py"):
        await ctx.send("Cuma bisa review file `.py`")
        return

    if file.size > 50_000:
        await ctx.send("File terlalu besar (max 50KB)")
        return

    try:
        content = await file.read()
        code = content.decode("utf-8")
    except Exception as e:
        await ctx.send(f"Gagal baca file: {e}")
        return

    user_prompt = f"Review this Python code:\n\n```python\n{code}\n```"
    if question:
        user_prompt += f"\n\nFokus ke: {question}"

    async with ctx.typing():
        try:
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an expert Python code reviewer. "
                            "Don't just find bugs — review code quality, readability, "
                            "best practices, and suggest improvements. "
                            "Be constructive and specific."
                        )
                    },
                    {
                        "role": "user",
                        "content": user_prompt
                    }
                ]
            )

            reply = response.choices[0].message.content

            if len(reply) <= 2000:
                await ctx.reply(reply)
            else:
                file_output = io.BytesIO(reply.encode("utf-8"))
                await ctx.reply(
                    "Hasil review terlalu panjang, nih filenya 📄",
                    file=discord.File(file_output, filename="review_result.txt")
                )

        except Exception as e:
            await ctx.reply(f"AI error: {e}")

#buat cek berapa lama bot udah on
@bot.command()
async def uptime(ctx):
    uptime_seconds = int(time.time() - START_TIME)

    days = uptime_seconds // 86400
    hours = uptime_seconds // 3600
    minutes = (uptime_seconds % 3600) // 60
    seconds = uptime_seconds % 60
    
    embed = discord.Embed(
        title="⏱️ Uptime Enki",
        description=f"**{days}h {hours}j {minutes}m {seconds}d**",
        color=0x00ff99  # warna hijau, bisa diganti
    )
    embed.set_footer(text="Enki v1.0")
    
    await ctx.send(embed=embed)

#buat set prefix
@bot.command()
@commands.has_permissions(administrator=True)
async def setprefix(ctx, prefix: str):
    set_prefix(str(ctx.guild.id), prefix)
    embed = discord.Embed(
        title="✅ Prefix Updated",
        description=f"Prefix sekarang: `{prefix}`",
        color=0x00ff99
    )
    await ctx.send(embed=embed)

#buat cek statistik udah pake enki berapa lama
@bot.command()
async def stats(ctx):
    user_id = str(ctx.author.id)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # total pesan user
    c.execute("SELECT COUNT(*) FROM memory WHERE user_id = ?", (user_id,))
    total = c.fetchone()[0]
    
    # total pesan user ke AI (role user)
    c.execute("SELECT COUNT(*) FROM memory WHERE user_id = ? AND role = 'user'", (user_id,))
    total_user = c.fetchone()[0]
    
    # total balasan AI
    c.execute("SELECT COUNT(*) FROM memory WHERE user_id = ? AND role = 'assistant'", (user_id,))
    total_ai = c.fetchone()[0]
    
    conn.close()
    
    embed = discord.Embed(
        title="📊 Stats Kamu",
        color=0x00ff99
    )
    embed.add_field(name="Total Pesan", value=f"`{total}`", inline=True)
    embed.add_field(name="Pesan Kamu", value=f"`{total_user}`", inline=True)
    embed.add_field(name="Balasan Enki", value=f"`{total_ai}`", inline=True)
    embed.set_footer(text=f"Stats untuk {ctx.author.display_name}")
    
    await ctx.send(embed=embed)

if not TOKEN:
    print("ERROR: TOKEN tidak ditemukan!")
else:
    bot.run(TOKEN)
