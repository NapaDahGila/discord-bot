import os
import json
import io
import sqlite3
import time
import discord
import aiohttp
import random
import asyncio
import pytz
from datetime import datetime
from discord.ext import commands
from groq import Groq

TOKEN = os.getenv("TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")
WEATHER_KEY = os.getenv("WEATHER_KEY")

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
    c.execute("""
    CREATE TABLE IF NOT EXISTS wack_scores (
        user_id   TEXT PRIMARY KEY,
        username  TEXT NOT NULL,
        best      INTEGER DEFAULT 0,
        total     INTEGER DEFAULT 0,
        games     INTEGER DEFAULT 0
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

def save_wack_score(user_id: str, username: str, skor: int, total: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO wack_scores (user_id, username, best, total, games)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            username = ?,
            best = MAX(best, ?),
            total = total + ?,
            games = games + 1
    """, (user_id, username, skor, skor, username, skor, skor))
    conn.commit()
    conn.close()

def get_leaderboard():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username, best, total, games FROM wack_scores ORDER BY best DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    return rows

init_db()
afk_users = {}

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
                            "Jangan sebut OpenAI atau model apapun."
                            f"Sekarang waktu Indonesia Barat: {sekarang}."
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

#buat khusus channel namanya "enki" auto reply
@bot.event
async def on_message(message):

    if message.author == bot.user:
        return

    if message.mentions:
        for user in message.mentions:
            if user.id in afk_users:
                await message.channel.send(f"⚠️ {user.display_name} lagi AFK: `{afk_users[user.id]}`")

# cek kalo yang AFK balik
    if message.author.id in afk_users:
        del afk_users[message.author.id]
        embed = discord.Embed(
            description=f"Welcome back {message.author.display_name}! AFK kamu udah dihapus 👋",color=0x00ff99)
        await message.channel.send(embed=embed)

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
                        "Jangan sebut OpenAI atau model apapun."
                        f"Sekarang waktu Indonesia Barat: {sekarang}."
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

@bot.command()
async def cuaca(ctx, *, kota: str):
    if not WEATHER_KEY:
        await ctx.send("API key cuaca belum diset.")
        return

    async with aiohttp.ClientSession() as session:
        url = f"http://api.openweathermap.org/data/2.5/weather?q={kota}&appid={WEATHER_KEY}&units=metric&lang=id"
        async with session.get(url) as resp:
            print(f"Status: {resp.status}")  # ← tambahin ini
            print(f"URL: {url}")             # ← sama ini
            if resp.status != 200:
                await ctx.send(f"Kota `{kota}` ga ketemu 😅")
                return
            data = await resp.json()

    cuaca_desc = data["weather"][0]["description"]
    suhu = data["main"]["temp"]
    suhu_min = data["main"]["temp_min"]
    suhu_max = data["main"]["temp_max"]
    kelembaban = data["main"]["humidity"]
    angin = data["wind"]["speed"]

    embed = discord.Embed(
        title=f"🌤️ Cuaca di {kota.title()}",
        color=0x00ff99
    )
    embed.add_field(name="Kondisi", value=f"`{cuaca_desc}`", inline=False)
    embed.add_field(name="🌡️ Suhu", value=f"`{suhu}°C`", inline=True)
    embed.add_field(name="🔽 Min", value=f"`{suhu_min}°C`", inline=True)
    embed.add_field(name="🔼 Max", value=f"`{suhu_max}°C`", inline=True)
    embed.add_field(name="💧 Kelembaban", value=f"`{kelembaban}%`", inline=True)
    embed.add_field(name="💨 Angin", value=f"`{angin} m/s`", inline=True)

    await ctx.send(embed=embed)

@bot.command()
async def translate(ctx, bahasa: str, *, teks: str):
    """Translate teks ke bahasa lain"""
    async with aiohttp.ClientSession() as session:
        url = f"https://api.mymemory.translated.net/get?q={teks}&langpair=id|{bahasa}"
        async with session.get(url) as resp:
            if resp.status != 200:
                await ctx.send("Gagal translate 😅")
                return
            data = await resp.json()

    hasil = data["responseData"]["translatedText"]
    
    embed = discord.Embed(
        title="🌐 Translate",
        color=0x00ff99
    )
    embed.add_field(name="Teks Asli", value=f"`{teks}`", inline=False)
    embed.add_field(name="Hasil", value=f"`{hasil}`", inline=False)
    embed.set_footer(text=f"id → {bahasa}")
    
    await ctx.send(embed=embed)

@bot.command()
async def ball(ctx, *, pertanyaan: str):
    jawaban = [
        "Iya, pasti! 🎱",
        "Kemungkinan besar iya.",
        "Tanda-tandanya bagus.",
        "Coba lagi nanti 🤔",
        "Ga bisa dipastiin sekarang.",
        "Jangan terlalu berharap 😅",
        "Ga mungkin.",
        "Kayaknya sih ngga.",
        "Absolutely not 💀",
        "Bro yakin mau tau jawabannya? 😂",
        "Tanya lagi nanti, gw lagi males mikir.",
        "Hmm... iya deh, tapi jangan nyalahin gw kalo salah.",
        "Tanya yang lain deh",
    ]

    hasil = random.choice(jawaban)

    embed = discord.Embed(
        title="🎱 8Ball",
        color=0x00ff99
    )
    embed.add_field(name="Pertanyaan", value=f"`{pertanyaan}`", inline=False)
    embed.add_field(name="Jawaban", value=hasil, inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def afk(ctx, *, alasan: str = "AFK"):
    afk_users[ctx.author.id] = alasan
    embed = discord.Embed(
        title="💤 AFK",
        description=f"{ctx.author.display_name} sekarang AFK: `{alasan}`",
        color=0x00ff99
    )
    await ctx.send(embed=embed)

@bot.command()
async def wack(ctx):
    
    skor = 0
    ronde = 5

    await ctx.send("🎮 **Whack-a-Mole dimulai!** Klik reaction 🐭 secepat mungkin!\n3...")
    await asyncio.sleep(1)
    await ctx.send("2...")
    await asyncio.sleep(1)
    await ctx.send("1...")
    await asyncio.sleep(1)

    for i in range(ronde):
        # posisi tikus random dari 5 lubang
        posisi = random.randint(0, 4)
        lubang = ["🕳️", "🕳️", "🕳️", "🕳️", "🕳️"]
        lubang[posisi] = "🐭"

        papan = " ".join(lubang)
        pesan = await ctx.send(f"**Ronde {i+1}/{ronde}**\n{papan}")

        # tambahin reaction sesuai posisi
        reactions = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        for r in reactions:
            await pesan.add_reaction(r)

        def check(reaction, user):
            return user == ctx.author and str(reaction.emoji) in reactions and reaction.message.id == pesan.id

        try:
            reaction, user = await bot.wait_for("reaction_add", timeout=3.0, check=check)
            if reactions.index(str(reaction.emoji)) == posisi:
                skor += 1
                await ctx.send(f"✅ Bener! +1 | Skor: {skor}")
            else:
                await ctx.send(f"❌ Salah! Tikusnya di {reactions[posisi]}")
        except asyncio.TimeoutError:
            await ctx.send(f"⏱️ Timeout! Tikusnya di {reactions[posisi]}")

        await asyncio.sleep(1)

    embed = discord.Embed(
        title="🎮 Game Selesai!",
        description=f"Skor akhir: **{skor}/{ronde}**",
        color=0x00ff99
    )
    if skor == ronde:
        embed.set_footer(text="Sempurna! 🏆")
    elif skor >= ronde // 2:
        embed.set_footer(text="Lumayan! 👍")
    else:
        embed.set_footer(text="Latihan lagi bro 😂")

    save_wack_score(str(ctx.author.id), ctx.author.display_name, skor, ronde)

    await ctx.send(embed=embed)

@bot.command()
async def leaderboard(ctx):
    data = get_leaderboard()

    if not data:
        await ctx.send("Belum ada yang main `!wack` 😅")
        return

    embed = discord.Embed(
        title="🏆 Leaderboard Whack-a-Mole",
        color=0x00ff99
    )

    medals = ["🥇", "🥈", "🥉"]
    for i, (username, best, total, games) in enumerate(data):
        medal = medals[i] if i < 3 else f"`{i+1}.`"
        embed.add_field(
            name=f"{medal} {username}",
            value=f"Best: `{best}` | Total: `{total}` | Games: `{games}`",
            inline=False
        )

    await ctx.send(embed=embed)


if not TOKEN:
    print("ERROR: TOKEN tidak ditemukan!")
else:
    bot.run(TOKEN)
