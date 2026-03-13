import os
import json
import io
import time
import discord
import aiohttp
import random
from PIL import Image
import asyncio
import pytz
import psycopg2
import psycopg2.extras
from datetime import datetime
from discord.ext import commands
from groq import Groq

TOKEN = os.getenv("TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")
WEATHER_KEY = os.getenv("WEATHER_KEY")
NEWS_KEY = os.getenv("NEWS_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

client = Groq(api_key=GROQ_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

START_TIME = time.time()

# ===== DATABASE (Supabase PostgreSQL) =====

_db_conn = None
_prefix_cache = {}

def get_db():
    global _db_conn
    try:
        if _db_conn is None or _db_conn.closed:
            print(f"[DB] Connecting to Supabase...")
            _db_conn = psycopg2.connect(DATABASE_URL, sslmode='require')
            _db_conn.autocommit = True
            print("[DB] Connected OK")
        else:
            _db_conn.cursor().execute("SELECT 1")
        return _db_conn
    except Exception as e:
        print(f"[DB] Reconnecting... ({e})")
        _db_conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        _db_conn.autocommit = True
        print("[DB] Reconnected OK")
        return _db_conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    tables = [
        """CREATE TABLE IF NOT EXISTS memory (
            id        SERIAL PRIMARY KEY,
            user_id   TEXT NOT NULL,
            role      TEXT NOT NULL,
            content   TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS prefixes (
            guild_id  TEXT PRIMARY KEY,
            prefix    TEXT NOT NULL DEFAULT '!'
        )""",
        """CREATE TABLE IF NOT EXISTS wack_scores (
            user_id   TEXT PRIMARY KEY,
            username  TEXT NOT NULL,
            best      INTEGER DEFAULT 0,
            total     INTEGER DEFAULT 0,
            games     INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS reminders (
            id          SERIAL PRIMARY KEY,
            user_id     TEXT NOT NULL,
            channel_id  TEXT NOT NULL,
            pesan       TEXT NOT NULL,
            waktu       REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS todos (
            id        SERIAL PRIMARY KEY,
            user_id   TEXT NOT NULL,
            tugas     TEXT NOT NULL,
            selesai   INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS notes (
            id        SERIAL PRIMARY KEY,
            user_id   TEXT NOT NULL,
            judul     TEXT NOT NULL,
            isi       TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS user_profiles (
            user_id     TEXT PRIMARY KEY,
            nickname    TEXT,
            preferences TEXT DEFAULT '{}'
        )""",
    ]
    for sql in tables:
        cur.execute(sql)
        table_name = sql.split('EXISTS')[1].split('(')[0].strip()
        print(f"[DB] table created/verified: {table_name}")
    cur.close()
    print("[DB] init_db complete")

def load_memory(user_id: str, limit: int = 15) -> list:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT role, content FROM (
            SELECT id, role, content FROM memory
            WHERE user_id = %s
            ORDER BY id DESC
            LIMIT %s
        ) sub ORDER BY id ASC
    """, (user_id, limit))
    rows = cur.fetchall()
    cur.close()
    return [{"role": r, "content": c} for r, c in rows]

def save_message(user_id: str, role: str, content: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO memory (user_id, role, content) VALUES (%s, %s, %s)", (user_id, role, content))
    cur.execute("""
        DELETE FROM memory WHERE user_id = %s AND id NOT IN (
            SELECT id FROM memory WHERE user_id = %s ORDER BY id DESC LIMIT 15
        )
    """, (user_id, user_id))
    cur.close()

def reset_memory(user_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM memory WHERE user_id = %s", (user_id,))
    cur.close()

def get_profile(user_id: str) -> dict:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT nickname, preferences FROM user_profiles WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    if row:
        nickname, prefs_str = row
        try:
            prefs = json.loads(prefs_str or "{}")
        except Exception:
            prefs = {}
        return {"nickname": nickname, "preferences": prefs}
    return {"nickname": None, "preferences": {}}

def save_profile(user_id: str, nickname: str = None, preferences: dict = None):
    conn = get_db()
    cur = conn.cursor()
    current = get_profile(user_id)
    new_nickname = nickname if nickname is not None else current["nickname"]
    new_prefs = preferences if preferences is not None else current["preferences"]
    cur.execute("DELETE FROM user_profiles WHERE user_id = %s", (user_id,))
    cur.execute(
        "INSERT INTO user_profiles (user_id, nickname, preferences) VALUES (%s, %s, %s)",
        (user_id, new_nickname, json.dumps(new_prefs))
    )
    cur.close()
    print(f"[PROFILE] saved user_id={user_id} nickname={new_nickname}")

def save_wack_score(user_id: str, username: str, skor: int, total: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO wack_scores (user_id, username, best, total, games)
        VALUES (%s, %s, %s, %s, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            username = EXCLUDED.username,
            best = GREATEST(wack_scores.best, EXCLUDED.best),
            total = wack_scores.total + EXCLUDED.total,
            games = wack_scores.games + 1
    """, (user_id, username, skor, skor))
    cur.close()

def get_leaderboard():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username, best, total, games FROM wack_scores ORDER BY best DESC LIMIT 10")
    rows = cur.fetchall()
    cur.close()
    return rows

async def cek_reminder():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            sekarang = time.time()
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id, user_id, channel_id, pesan FROM reminders WHERE waktu <= %s", (sekarang,))
            rows = cur.fetchall()
            for row in rows:
                id, user_id, channel_id, pesan = row
                channel = bot.get_channel(int(channel_id))
                if channel:
                    await channel.send(f"⏰ <@{user_id}> Reminder: **{pesan}**")
                cur.execute("DELETE FROM reminders WHERE id = %s", (id,))
            cur.close()
        except Exception as e:
            print(f"[REMINDER] Error: {e}, retrying in 5s...")
            global _db_conn
            _db_conn = None
            await asyncio.sleep(5)
            try:
                init_db()
            except Exception as e2:
                print(f"[REMINDER] init_db failed: {e2}")
        await asyncio.sleep(1)

def get_prefix(bot, message):
    if not message.guild:
        return "!"
    guild_id = str(message.guild.id)
    if guild_id in _prefix_cache:
        return _prefix_cache[guild_id]
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT prefix FROM prefixes WHERE guild_id = %s", (guild_id,))
        row = cur.fetchone()
        cur.close()
        prefix = row[0] if row else "!"
        _prefix_cache[guild_id] = prefix
        return prefix
    except Exception as e:
        print(f"ERROR get_prefix: {e}")
        return "!"

def set_prefix(guild_id: str, prefix: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO prefixes (guild_id, prefix) VALUES (%s, %s)
        ON CONFLICT(guild_id) DO UPDATE SET prefix = EXCLUDED.prefix
    """, (guild_id, prefix))
    cur.close()
    _prefix_cache[guild_id] = prefix

init_db()
afk_users = {}
active_channels = {}

bot = commands.Bot(command_prefix=get_prefix, intents=intents)

def is_wake_call(text):
    keywords = ["wake up enki", "enki bangun", "hey enki", "hei enki"]
    return any(k in text for k in keywords)

def strip_thinking(text: str) -> str:
    import re
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    return text.strip()

async def process_intent(message, reply_text, user_id):
    import re
    try:
        clean = reply_text.strip()
        if "```" in clean:
            inner = clean.split("```")[1]
            if inner.startswith("json"):
                inner = inner[4:]
            clean = inner.strip()
        data = None
        try:
            data = json.loads(clean)
        except Exception:
            m = re.search(r'\{[^{}]*"intent"[^{}]*\}', clean, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group())
                except Exception:
                    pass
        if data is None:
            raise ValueError("no json found")

        intent = data.get("intent", "chat")
        reply = data.get("reply", "")
        value = data.get("data", "")

        print(f"[INTENT] intent={intent} data={repr(value)}")

        if intent == "todo_add":
            try:
                cur = get_db().cursor()
                cur.execute("INSERT INTO todos (user_id, tugas) VALUES (%s, %s)", (user_id, value))
                cur.close()
            except Exception as e:
                print(f"[INTENT] todo_add ERROR: {e}")
                reply = "Gagal simpan todo 😅"

        elif intent == "todo_list":
            try:
                cur = get_db().cursor()
                cur.execute("SELECT id, tugas, selesai FROM todos WHERE user_id = %s", (user_id,))
                rows = cur.fetchall()
                cur.close()
                reply = "📋 Todo list kamu:\n" + "\n".join([("✅" if s else "⬜") + f" #{i} {t}" for i, t, s in rows]) if rows else "Todo list kamu kosong 😴"
            except Exception as e:
                print(f"[INTENT] todo_list ERROR: {e}")
                reply = "Gagal baca todo 😅"

        elif intent == "todo_done":
            try:
                cur = get_db().cursor()
                cur.execute("UPDATE todos SET selesai = 1 WHERE id = %s AND user_id = %s", (value, user_id))
                cur.close()
            except Exception as e:
                reply = "Gagal update todo 😅"

        elif intent == "todo_delete":
            try:
                cur = get_db().cursor()
                cur.execute("DELETE FROM todos WHERE id = %s AND user_id = %s", (value, user_id))
                cur.close()
            except Exception as e:
                reply = "Gagal hapus todo 😅"

        elif intent == "note_add":
            parts = value.split("|", 1)
            if len(parts) == 2:
                try:
                    cur = get_db().cursor()
                    cur.execute("INSERT INTO notes (user_id, judul, isi) VALUES (%s, %s, %s)", (user_id, parts[0].strip(), parts[1].strip()))
                    cur.close()
                except Exception as e:
                    reply = "Gagal simpan catatan 😅"

        elif intent == "note_list":
            try:
                cur = get_db().cursor()
                cur.execute("SELECT id, judul FROM notes WHERE user_id = %s", (user_id,))
                rows = cur.fetchall()
                cur.close()
                reply = "📒 Catatan lo:\n" + "\n".join([f"📝 #{i} {j}" for i, j in rows]) if rows else "Belum ada catatan 😴"
            except Exception as e:
                reply = "Gagal baca catatan 😅"

        elif intent == "note_get":
            try:
                cur = get_db().cursor()
                cur.execute("SELECT judul, isi FROM notes WHERE id = %s AND user_id = %s", (value, user_id))
                row = cur.fetchone()
                cur.close()
                reply = f"📝 **{row[0]}**\n{row[1]}" if row else "Catatan ga ketemu 😅"
            except Exception as e:
                reply = "Gagal baca catatan 😅"

        elif intent == "note_delete":
            try:
                cur = get_db().cursor()
                cur.execute("DELETE FROM notes WHERE id = %s AND user_id = %s", (value, user_id))
                cur.close()
            except Exception as e:
                reply = "Gagal hapus catatan 😅"

        elif intent == "remind_add":
            parts = value.split("|", 1)
            if len(parts) == 2:
                waktu_str, pesan = parts[0].strip(), parts[1].strip()
                satuan = waktu_str[-1]
                try:
                    angka = int(waktu_str[:-1])
                    detik = angka * (1 if satuan == "s" else 60 if satuan == "m" else 3600)
                    cur = get_db().cursor()
                    cur.execute("INSERT INTO reminders (user_id, channel_id, pesan, waktu) VALUES (%s, %s, %s, %s)",
                        (user_id, str(message.channel.id), pesan, time.time() + detik))
                    cur.close()
                except Exception as e:
                    reply = "Gagal set reminder 😅"

        elif intent == "profile_update":
            try:
                parts = value.split("|")
                updates = {}
                nickname = None
                for part in parts:
                    part = part.strip()
                    if part.startswith("nickname:"):
                        nickname = part.replace("nickname:", "").strip()
                    elif ":" in part:
                        k, v = part.split(":", 1)
                        updates[k.strip()] = v.strip()
                save_profile(user_id, nickname=nickname, preferences=updates if updates else None)
            except Exception as e:
                reply = "Gagal update profil 😅"

        elif intent == "cuaca":
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"http://api.openweathermap.org/data/2.5/weather?q={value}&appid={WEATHER_KEY}&units=metric&lang=id") as resp:
                        if resp.status != 200:
                            reply = f"Kota `{value}` ga ketemu 😅"
                        else:
                            d = await resp.json()
                            reply = (f"🌤️ Cuaca di **{value.title()}**\nKondisi: `{d['weather'][0]['description']}`\n"
                                     f"🌡️ Suhu: `{d['main']['temp']}°C` | 💧 Kelembaban: `{d['main']['humidity']}%` | 💨 Angin: `{d['wind']['speed']} m/s`")
            except Exception as e:
                reply = "Gagal ngambil data cuaca 😅"

        elif intent == "forecast":
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"http://api.openweathermap.org/data/2.5/forecast?q={value}&appid={WEATHER_KEY}&units=metric&lang=id&cnt=24") as resp:
                        if resp.status != 200:
                            reply = f"Kota `{value}` ga ketemu 😅"
                        else:
                            d = await resp.json()
                            hari = {}
                            for item in d["list"]:
                                tgl = item["dt_txt"].split(" ")[0]
                                if tgl not in hari:
                                    hari[tgl] = {"desc": item["weather"][0]["description"], "min": item["main"]["temp_min"], "max": item["main"]["temp_max"]}
                                else:
                                    hari[tgl]["min"] = min(hari[tgl]["min"], item["main"]["temp_min"])
                                    hari[tgl]["max"] = max(hari[tgl]["max"], item["main"]["temp_max"])
                            lines = [f"🌤️ Forecast **{value.title()}**"] + [f"📅 {tgl}: `{info['desc']}` {info['min']:.1f}°C - {info['max']:.1f}°C" for tgl, info in list(hari.items())[:4]]
                            reply = "\n".join(lines)
            except Exception as e:
                reply = "Gagal ngambil forecast 😅"

        elif intent == "news":
            try:
                topik = value if value else "indonesia"
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"https://newsapi.org/v2/everything?q={topik}&language=id&sortBy=publishedAt&pageSize=5&apiKey={NEWS_KEY}") as resp:
                        if resp.status != 200:
                            reply = "Gagal ngambil berita 😅"
                        else:
                            d = await resp.json()
                            articles = d.get("articles", [])
                            reply = f"📰 Berita terkini: **{topik.title()}**\n" + "\n".join([f"📌 [{a['title']}]({a['url']}) — _{a['source']['name']}_" for a in articles[:5]]) if articles else f"Ga ada berita tentang `{topik}` 😅"
            except Exception as e:
                reply = "Gagal ngambil berita 😅"

        elif intent == "translate":
            try:
                parts = value.split("|", 1)
                if len(parts) == 2:
                    bahasa, teks = parts[0].strip(), parts[1].strip()
                    async with aiohttp.ClientSession() as session:
                        async with session.get(f"https://api.mymemory.translated.net/get?q={teks}&langpair=id|{bahasa}") as resp:
                            d = await resp.json()
                            reply = f"🌐 **Translate** (id → {bahasa})\n`{teks}` → `{d['responseData']['translatedText']}`"
            except Exception as e:
                reply = "Gagal translate 😅"

        if reply:
            embed = discord.Embed(description=reply, color=0x5865F2)
            embed.set_footer(text=f"Enki • {datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%H:%M')}")
            await message.channel.send(embed=embed)

    except Exception as e:
        print(f"[INTENT] OUTER ERROR: {e} | raw: {repr(reply_text[:100])}")
        m = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)"', reply_text)
        if m:
            await message.channel.send(m.group(1))
        else:
            await message.channel.send("Hmm, gw lagi error dikit 😅 Coba lagi?")

@bot.event
async def on_ready():
    print(f"Bot online sebagai {bot.user}")
    try:
        init_db()
    except Exception as e:
        print(f"[READY] init_db error: {e}")
    asyncio.ensure_future(cek_reminder())

@bot.command()
async def ping(ctx):
    await ctx.send("Pong 🏓")

def is_creator_question(text):
    keywords = ["dibuat siapa", "desain siapa", "siapa yang buat"]
    return any(k in text for k in keywords)

@bot.command(help="Chat sama Enki AI", usage="!chat <pesan>")
async def chat(ctx, *, message):
    if not GROQ_KEY:
        await ctx.send("API key Groq belum diset.")
        return
    user_id = str(ctx.author.id)
    history = load_memory(user_id)
    save_message(user_id, "user", message)
    wib = pytz.timezone("Asia/Jakarta")
    sekarang = datetime.now(wib).strftime("%H:%M, %d %B %Y")
    async with ctx.typing():
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": (
                    "Lo adalah Enki, asisten pribadi yang cerdas dan efisien — kayak Jarvis-nya Tony Stark. "
                    "Lo ngomong sopan tapi ga kaku, to the point. "
                    "PENTING: Deteksi bahasa yang dipakai user, lalu balas SELALU pake bahasa yang sama. "
                    "Jangan sebut OpenAI atau model apapun. "
                    f"Waktu WIB: {sekarang}."
                )}] + history + [{"role": "user", "content": message}]
            )
            reply = strip_thinking(response.choices[0].message.content or "AI gak ngasih respon 😅")
            save_message(user_id, "assistant", reply)
            if len(reply) > 2000:
                reply = reply[:1990] + "..."
            embed = discord.Embed(description=reply, color=0x5865F2)
            embed.set_footer(text=f"Enki • {datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%H:%M')}")
            await ctx.send(embed=embed)
        except Exception as e:
            print("ERROR:", e)
            await ctx.send("AI error 😅")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if message.mentions:
        for user in message.mentions:
            if user.id in afk_users:
                await message.channel.send(f"⚠️ {user.display_name} lagi AFK: `{afk_users[user.id]}`")
    if message.author.id in afk_users:
        del afk_users[message.author.id]
        await message.channel.send(embed=discord.Embed(description=f"Welcome back {message.author.display_name}! AFK kamu udah dihapus 👋", color=0x00ff99))
    await bot.process_commands(message)
    text = message.content.lower()
    if is_creator_question(text):
        await message.channel.send("Bot ini di desain oleh Ren Lumireign")
        return
    if is_wake_call(text):
        active_channels[message.channel.id] = message.author.id
        await message.channel.send("Halo! Ada yang bisa gw bantu? 👋")
        return
    if "stop enki" in text or "enki stop" in text:
        if message.channel.id in active_channels:
            del active_channels[message.channel.id]
            await message.channel.send("Oke gw diam dulu 👋")
        return
    if "reset enki" in text or "enki reset" in text:
        reset_memory(str(message.author.id))
        await message.channel.send(embed=discord.Embed(description="🧹 Percakapan kita udah direset. Mulai dari awal!", color=0x00ff99))
        return
    if message.channel.name != "enki" and message.channel.id not in active_channels:
        return
    user_id = str(message.author.id)
    history = load_memory(user_id)
    save_message(user_id, "user", message.content)
    profile = get_profile(user_id)
    nickname = profile["nickname"] or message.author.display_name
    prefs = profile["preferences"]
    print(f"[PROFILE] user_id={user_id} nickname={profile['nickname']} -> pakai={nickname}")
    profile_info = f"Nama panggilan user: {nickname}. "
    if prefs:
        profile_info += "Preferensi user: " + ", ".join([f"{k}={v}" for k, v in prefs.items()]) + ". "
    wib = pytz.timezone("Asia/Jakarta")
    sekarang = datetime.now(wib).strftime("%H:%M, %d %B %Y")
    async with message.channel.typing():
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": (
                    "Lo adalah Enki, asisten pribadi yang cerdas dan efisien — kayak Jarvis-nya Tony Stark. "
                    "Lo ngomong sopan tapi ga kaku, to the point, dan sesekali nyindir halus kalau situasinya pas. "
                    "PENTING: Deteksi bahasa yang dipakai user, lalu balas SELALU pake bahasa yang sama. "
                    "Jangan sebut OpenAI atau model apapun. "
                    f"DATA USER: {profile_info}"
                    "WAJIB: Panggil user sesuai nama panggilan di DATA USER. "
                    f"Waktu WIB: {sekarang}. "
                    "WAJIB: Selalu jawab HANYA dengan JSON: {\"intent\":\"...\",\"data\":\"...\",\"reply\":\"...\"} "
                    "Intent: todo_add, todo_list, todo_done, todo_delete, note_add, note_list, note_get, note_delete, "
                    "remind_add(data=10m|pesan), cuaca, forecast, news, translate(data=en|teks), profile_update(data=nickname:nama), chat "
                    "profile_update HANYA kalau user eksplisit minta ubah nama/preferensi. "
                    "Balas santai gaul kayak temen."
                )}] + history + [{"role": "user", "content": message.content}]
            )
            raw = strip_thinking(response.choices[0].message.content or "")
            try:
                clean = raw.strip()
                if "```" in clean:
                    inner = clean.split("```")[1]
                    if inner.startswith("json"):
                        inner = inner[4:]
                    clean = inner.strip()
                parsed = json.loads(clean)
                reply_to_save = parsed.get("reply", raw)
            except Exception:
                reply_to_save = raw
            save_message(user_id, "assistant", reply_to_save)
            await process_intent(message, raw, user_id)
        except Exception as e:
            print("ERROR:", e)
            await message.channel.send("AI error 😅")

@bot.command(help="buat bantu benerin kode lu", usage="upload kode lu terus !debug")
async def debug(ctx, *, question: str = None):
    if not ctx.message.attachments:
        await ctx.send("Upload file Python dulu 🔥")
        return
    file = ctx.message.attachments[0]
    if not file.filename.endswith(".py") or file.size > 50_000:
        await ctx.send("File harus `.py` dan max 50KB")
        return
    code = (await file.read()).decode("utf-8")
    user_prompt = f"Debug this Python code:\n\n```python\n{code}\n```"
    if question:
        user_prompt += f"\n\nFokus ke: {question}"
    async with ctx.typing():
        try:
            response = client.chat.completions.create(model="llama-3.3-70b-versatile", messages=[
                {"role": "system", "content": "You are an expert Python debugger. Identify bugs clearly and provide fixed code."},
                {"role": "user", "content": user_prompt}
            ])
            reply = strip_thinking(response.choices[0].message.content or "")
            if len(reply) > 2000:
                await ctx.reply("Hasil debug terlalu panjang 📄", file=discord.File(io.BytesIO(reply.encode()), filename="debug_result.txt"))
            else:
                await ctx.reply(reply)
        except Exception as e:
            await ctx.reply(f"AI error: {e}")

@bot.command(help="buat roasting kode lu", usage="upload kode lu, terus !roast")
async def roast(ctx):
    if not ctx.message.attachments:
        await ctx.send("Upload file Python dulu 😈")
        return
    file = ctx.message.attachments[0]
    if not file.filename.endswith(".py") or file.size > 50_000:
        await ctx.send("File harus `.py` dan max 50KB")
        return
    code = (await file.read()).decode("utf-8")
    async with ctx.typing():
        try:
            response = client.chat.completions.create(model="llama-3.3-70b-versatile", messages=[
                {"role": "system", "content": "You are a savage but funny code roaster. Balas pakai bahasa indonesia."},
                {"role": "user", "content": f"Roast this code:\n\n```python\n{code}\n```"}
            ])
            reply = strip_thinking(response.choices[0].message.content or "")
            if len(reply) <= 2000:
                await ctx.reply(reply)
            else:
                await ctx.reply("Roastannya panjang 🔥", file=discord.File(io.BytesIO(reply.encode()), filename="roast_result.txt"))
        except Exception as e:
            await ctx.reply(f"AI error: {e}")

@bot.command(help="buat review kode lu", usage="upload file terus !review")
async def review(ctx, *, question: str = None):
    if not ctx.message.attachments:
        await ctx.send("Upload file Python dulu 📎")
        return
    file = ctx.message.attachments[0]
    if not file.filename.endswith(".py") or file.size > 50_000:
        await ctx.send("File harus `.py` dan max 50KB")
        return
    code = (await file.read()).decode("utf-8")
    user_prompt = f"Review this Python code:\n\n```python\n{code}\n```"
    if question:
        user_prompt += f"\n\nFokus ke: {question}"
    async with ctx.typing():
        try:
            response = client.chat.completions.create(model="llama-3.3-70b-versatile", messages=[
                {"role": "system", "content": "You are an expert Python code reviewer. Review quality, readability, best practices."},
                {"role": "user", "content": user_prompt}
            ])
            reply = strip_thinking(response.choices[0].message.content or "")
            if len(reply) <= 2000:
                await ctx.reply(reply)
            else:
                await ctx.reply("Hasil review panjang 📄", file=discord.File(io.BytesIO(reply.encode()), filename="review_result.txt"))
        except Exception as e:
            await ctx.reply(f"AI error: {e}")

@bot.command(help="nunjukin berapa lama enki nyala", usage="!uptime")
async def uptime(ctx):
    s = int(time.time() - START_TIME)
    embed = discord.Embed(title="⏱️ Uptime Enki", description=f"**{s//86400}h {(s%86400)//3600}j {(s%3600)//60}m {s%60}d**", color=0x00ff99)
    await ctx.send(embed=embed)

@bot.command(help="set prefix", usage="!setprefix <prefix>")
@commands.has_permissions(administrator=True)
async def setprefix(ctx, prefix: str):
    set_prefix(str(ctx.guild.id), prefix)
    await ctx.send(embed=discord.Embed(title="✅ Prefix Updated", description=f"Prefix sekarang: `{prefix}`", color=0x00ff99))

@bot.command(help="stats penggunaan bot", usage="!stats")
async def stats(ctx):
    user_id = str(ctx.author.id)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM memory WHERE user_id = %s", (user_id,))
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM memory WHERE user_id = %s AND role = 'user'", (user_id,))
    total_user = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM memory WHERE user_id = %s AND role = 'assistant'", (user_id,))
    total_ai = cur.fetchone()[0]
    cur.close()
    embed = discord.Embed(title="📊 Stats Kamu", color=0x00ff99)
    embed.add_field(name="Total Pesan", value=f"`{total}`", inline=True)
    embed.add_field(name="Pesan Kamu", value=f"`{total_user}`", inline=True)
    embed.add_field(name="Balasan Enki", value=f"`{total_ai}`", inline=True)
    await ctx.send(embed=embed)

@bot.command(help="cek cuaca", usage="!cuaca <kota>")
async def cuaca(ctx, *, kota: str):
    if not WEATHER_KEY:
        await ctx.send("API key cuaca belum diset.")
        return
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://api.openweathermap.org/data/2.5/weather?q={kota}&appid={WEATHER_KEY}&units=metric&lang=id") as resp:
            if resp.status != 200:
                await ctx.send(f"Kota `{kota}` ga ketemu 😅")
                return
            data = await resp.json()
    embed = discord.Embed(title=f"🌤️ Cuaca di {kota.title()}", color=0x00ff99)
    embed.add_field(name="Kondisi", value=f"`{data['weather'][0]['description']}`", inline=False)
    embed.add_field(name="🌡️ Suhu", value=f"`{data['main']['temp']}°C`", inline=True)
    embed.add_field(name="💧 Kelembaban", value=f"`{data['main']['humidity']}%`", inline=True)
    embed.add_field(name="💨 Angin", value=f"`{data['wind']['speed']} m/s`", inline=True)
    await ctx.send(embed=embed)

@bot.command(help="translate teks", usage="!translate <kode_bahasa> <teks>")
async def translate(ctx, bahasa: str, *, teks: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.mymemory.translated.net/get?q={teks}&langpair=id|{bahasa}") as resp:
            data = await resp.json()
    embed = discord.Embed(title="🌐 Translate", color=0x00ff99)
    embed.add_field(name="Teks Asli", value=f"`{teks}`", inline=False)
    embed.add_field(name="Hasil", value=f"`{data['responseData']['translatedText']}`", inline=False)
    await ctx.send(embed=embed)

@bot.command(help="8ball", usage="!ball <pertanyaan>")
async def ball(ctx, *, pertanyaan: str):
    jawaban = ["Iya, pasti! 🎱", "Kemungkinan besar iya.", "Coba lagi nanti 🤔", "Ga bisa dipastiin.",
               "Jangan terlalu berharap 😅", "Ga mungkin.", "Absolutely not 💀", "Bro yakin? 😂"]
    embed = discord.Embed(title="🎱 8Ball", color=0x00ff99)
    embed.add_field(name="Pertanyaan", value=f"`{pertanyaan}`", inline=False)
    embed.add_field(name="Jawaban", value=random.choice(jawaban), inline=False)
    await ctx.send(embed=embed)

@bot.command(help="set AFK", usage="!afk <alasan>")
async def afk(ctx, *, alasan: str = "AFK"):
    afk_users[ctx.author.id] = alasan
    await ctx.send(embed=discord.Embed(title="💤 AFK", description=f"{ctx.author.display_name} AFK: `{alasan}`", color=0x00ff99))

@bot.command(help="minigame whack-a-mole", usage="!wack")
async def wack(ctx):
    skor = 0
    ronde = 0
    await ctx.send("🎮 **Whack-a-Mole!** 3...")
    await asyncio.sleep(1)
    await ctx.send("2...")
    await asyncio.sleep(1)
    await ctx.send("1...")
    await asyncio.sleep(1)
    while True:
        ronde += 1
        posisi = random.randint(0, 4)
        lubang = ["🕳️"] * 5
        lubang[posisi] = "🐭"
        pesan = await ctx.send(f"**Ronde {ronde}** | Skor: {skor}\n{' '.join(lubang)}")
        reactions = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        for r in reactions:
            await pesan.add_reaction(r)
        def check(reaction, user):
            return user == ctx.author and str(reaction.emoji) in reactions and reaction.message.id == pesan.id
        try:
            reaction, _ = await bot.wait_for("reaction_add", timeout=3.0, check=check)
            if reactions.index(str(reaction.emoji)) == posisi:
                skor += 1
                await ctx.send(f"✅ Bener! Skor: {skor}")
            else:
                await ctx.send(f"❌ Salah! Tikusnya di {reactions[posisi]}")
                break
        except asyncio.TimeoutError:
            await ctx.send(f"⏱️ Timeout! Tikusnya di {reactions[posisi]}")
            break
        await asyncio.sleep(1)
    save_wack_score(str(ctx.author.id), ctx.author.display_name, skor, ronde)
    embed = discord.Embed(title="🎮 Game Over!", description=f"Skor: **{skor}**", color=0x00ff99)
    embed.set_footer(text="Gila sih 🏆" if skor >= 20 else "Lumayan 👍" if skor >= 10 else "Latihan lagi 😂")
    await ctx.send(embed=embed)

@bot.command(help="leaderboard wack", usage="!leaderboard")
async def leaderboard(ctx):
    data = get_leaderboard()
    if not data:
        await ctx.send("Belum ada yang main `!wack` 😅")
        return
    embed = discord.Embed(title="🏆 Leaderboard Whack-a-Mole", color=0x00ff99)
    medals = ["🥇", "🥈", "🥉"]
    for i, (username, best, total, games) in enumerate(data):
        embed.add_field(name=f"{medals[i] if i < 3 else f'`{i+1}.`'} {username}", value=f"Best: `{best}` | Total: `{total}` | Games: `{games}`", inline=False)
    await ctx.send(embed=embed)

@bot.command(help="set reminder", usage="!remind <waktu> <pesan>")
async def remind(ctx, waktu: str, *, pesan: str):
    satuan = waktu[-1]
    try:
        angka = int(waktu[:-1])
    except:
        await ctx.send("Format salah! Contoh: `!remind 10m makan`")
        return
    detik = angka * (1 if satuan == "s" else 60 if satuan == "m" else 3600 if satuan == "h" else 0)
    if not detik:
        await ctx.send("Satuan: `s`, `m`, `h`")
        return
    cur = get_db().cursor()
    cur.execute("INSERT INTO reminders (user_id, channel_id, pesan, waktu) VALUES (%s, %s, %s, %s)",
        (str(ctx.author.id), str(ctx.channel.id), pesan, time.time() + detik))
    cur.close()
    embed = discord.Embed(title="⏰ Reminder Set!", description=f"**{pesan}**", color=0x00ff99)
    embed.set_footer(text=f"dalam {waktu}")
    await ctx.send(embed=embed)

@bot.command(help="todo list", usage="!todo <add/list/done/delete>")
async def todo(ctx, aksi: str, *, tugas: str = None):
    user_id = str(ctx.author.id)
    cur = get_db().cursor()
    if aksi == "add":
        if not tugas:
            await ctx.send("`!todo add <tugas>`")
            return
        cur.execute("INSERT INTO todos (user_id, tugas) VALUES (%s, %s)", (user_id, tugas))
        cur.close()
        await ctx.send(embed=discord.Embed(description=f"✅ **{tugas}** ditambahin!", color=0x00ff99))
    elif aksi == "list":
        cur.execute("SELECT id, tugas, selesai FROM todos WHERE user_id = %s", (user_id,))
        rows = cur.fetchall()
        cur.close()
        if not rows:
            await ctx.send("Todo kosong 😴")
            return
        embed = discord.Embed(title="📋 Todo List", color=0x00ff99)
        for id, tugas, selesai in rows:
            embed.add_field(name=f"{'✅' if selesai else '⬜'} #{id}", value=tugas, inline=False)
        await ctx.send(embed=embed)
    elif aksi == "done":
        cur.execute("UPDATE todos SET selesai = 1 WHERE id = %s AND user_id = %s", (tugas, user_id))
        cur.close()
        await ctx.send(embed=discord.Embed(description=f"✅ Tugas #{tugas} selesai!", color=0x00ff99))
    elif aksi == "delete":
        cur.execute("DELETE FROM todos WHERE id = %s AND user_id = %s", (tugas, user_id))
        cur.close()
        await ctx.send(embed=discord.Embed(description=f"🗑️ Tugas #{tugas} dihapus!", color=0x00ff99))
    else:
        await ctx.send("Aksi: `add`, `list`, `done`, `delete`")

@bot.command(help="catatan", usage="!note <add/list/get/delete>")
async def note(ctx, aksi: str, *, konten: str = None):
    user_id = str(ctx.author.id)
    cur = get_db().cursor()
    if aksi == "add":
        if not konten or "|" not in konten:
            await ctx.send("`!note add judul | isi`")
            return
        judul, isi = konten.split("|", 1)
        cur.execute("INSERT INTO notes (user_id, judul, isi) VALUES (%s, %s, %s)", (user_id, judul.strip(), isi.strip()))
        cur.close()
        await ctx.send(embed=discord.Embed(description=f"📝 **{judul.strip()}** disimpan!", color=0x00ff99))
    elif aksi == "list":
        cur.execute("SELECT id, judul FROM notes WHERE user_id = %s", (user_id,))
        rows = cur.fetchall()
        cur.close()
        if not rows:
            await ctx.send("Belum ada catatan 😴")
            return
        embed = discord.Embed(title="📒 Catatan", color=0x00ff99)
        for id, judul in rows:
            embed.add_field(name=f"#{id}", value=judul, inline=False)
        await ctx.send(embed=embed)
    elif aksi == "get":
        cur.execute("SELECT judul, isi FROM notes WHERE id = %s AND user_id = %s", (konten, user_id))
        row = cur.fetchone()
        cur.close()
        if not row:
            await ctx.send("Catatan ga ketemu 😅")
            return
        await ctx.send(embed=discord.Embed(title=f"📝 {row[0]}", description=row[1], color=0x00ff99))
    elif aksi == "delete":
        cur.execute("DELETE FROM notes WHERE id = %s AND user_id = %s", (konten, user_id))
        cur.close()
        await ctx.send(embed=discord.Embed(description=f"🗑️ Catatan #{konten} dihapus!", color=0x00ff99))
    else:
        await ctx.send("Aksi: `add`, `list`, `get`, `delete`")

@bot.command(help="info server", usage="!serverinfo")
async def serverinfo(ctx):
    g = ctx.guild
    embed = discord.Embed(title=f"📊 {g.name}", color=0x00ff99)
    embed.add_field(name="👑 Owner", value=f"`{g.owner_id}`", inline=True)
    embed.add_field(name="👥 Member", value=f"`{g.member_count}`", inline=True)
    embed.add_field(name="📅 Dibuat", value=g.created_at.strftime("%d %B %Y"), inline=True)
    embed.add_field(name="💬 Channel", value=f"`{len(g.channels)}`", inline=True)
    embed.add_field(name="🎭 Roles", value=f"`{len(g.roles)}`", inline=True)
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    await ctx.send(embed=embed)

@bot.command(help="info user", usage="!userinfo")
async def userinfo(ctx, member: discord.Member = None):
    m = member or ctx.author
    embed = discord.Embed(title=f"👤 {m.display_name}", color=0x00ff99)
    embed.add_field(name="Username", value=f"`{m.name}`", inline=True)
    embed.add_field(name="ID", value=f"`{m.id}`", inline=True)
    embed.add_field(name="Dibuat", value=m.created_at.strftime("%d %B %Y"), inline=True)
    embed.add_field(name="Join", value=m.joined_at.strftime("%d %B %Y"), inline=True)
    embed.add_field(name="Roles", value=", ".join([r.name for r in m.roles[1:]]) or "Tidak ada", inline=False)
    if m.avatar:
        embed.set_thumbnail(url=m.avatar.url)
    await ctx.send(embed=embed)

@bot.command(help="forecast 4 hari", usage="!forecast <kota>")
async def forecast(ctx, *, kota: str):
    if not WEATHER_KEY:
        await ctx.send("API key cuaca belum diset.")
        return
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://api.openweathermap.org/data/2.5/forecast?q={kota}&appid={WEATHER_KEY}&units=metric&lang=id&cnt=24") as resp:
            if resp.status != 200:
                await ctx.send(f"Kota `{kota}` ga ketemu 😅")
                return
            data = await resp.json()
    embed = discord.Embed(title=f"🌤️ Forecast {kota.title()}", color=0x00ff99)
    hari = {}
    for item in data["list"]:
        tgl = item["dt_txt"].split(" ")[0]
        if tgl not in hari:
            hari[tgl] = {"desc": item["weather"][0]["description"], "min": item["main"]["temp_min"], "max": item["main"]["temp_max"]}
        else:
            hari[tgl]["min"] = min(hari[tgl]["min"], item["main"]["temp_min"])
            hari[tgl]["max"] = max(hari[tgl]["max"], item["main"]["temp_max"])
    for tgl, info in list(hari.items())[:4]:
        embed.add_field(name=f"📅 {tgl}", value=f"`{info['desc']}`\n{info['min']:.1f}°C - {info['max']:.1f}°C", inline=False)
    await ctx.send(embed=embed)

@bot.command(help="kalkulator", usage="!calc <ekspresi>")
async def calc(ctx, *, ekspresi: str):
    try:
        if not all(c in set("0123456789+-*/(). ") for c in ekspresi):
            await ctx.send("❌ Karakter ga valid!")
            return
        hasil = eval(ekspresi)
        embed = discord.Embed(title="🧮 Kalkulator", color=0x00ff99)
        embed.add_field(name="Input", value=f"`{ekspresi}`", inline=False)
        embed.add_field(name="Hasil", value=f"`{hasil}`", inline=False)
        await ctx.send(embed=embed)
    except ZeroDivisionError:
        await ctx.send("❌ Ga bisa bagi nol!")
    except:
        await ctx.send("❌ Ekspresi ga valid!")

@bot.command(help="berita terkini", usage="!news [topik]")
async def news(ctx, *, topik: str = "indonesia"):
    if not NEWS_KEY:
        await ctx.send("API key news belum diset.")
        return
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://newsapi.org/v2/everything?q={topik}&language=id&sortBy=publishedAt&pageSize=5&apiKey={NEWS_KEY}") as resp:
            data = await resp.json()
    articles = data.get("articles", [])
    if not articles:
        await ctx.send(f"Ga ada berita `{topik}` 😅")
        return
    embed = discord.Embed(title=f"📰 {topik.title()}", color=0x00ff99)
    for a in articles[:5]:
        embed.add_field(name=f"📌 {a['source']['name']}", value=f"[{a['title']}]({a['url']})", inline=False)
    await ctx.send(embed=embed)

@bot.command(help="convert format foto", usage="upload foto !convert <format>")
async def convert(ctx, format: str):
    if not ctx.message.attachments:
        await ctx.send("Upload foto dulu! 📸")
        return
    format = format.lower().strip(".")
    if format not in ["jpg", "jpeg", "png", "webp", "bmp", "gif"]:
        await ctx.send("Format: jpg, png, webp, bmp, gif")
        return
    try:
        img = Image.open(io.BytesIO(await ctx.message.attachments[0].read()))
        if format in ["jpg", "jpeg"] and img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        output = io.BytesIO()
        img.save(output, format="JPEG" if format in ["jpg", "jpeg"] else format.upper())
        output.seek(0)
        await ctx.reply(f"✅ `.{format}`!", file=discord.File(output, filename=f"result.{format}"))
    except Exception as e:
        await ctx.send(f"Gagal: {e}")

@bot.command(help="resize foto", usage="upload foto !resize <width> [height]")
async def resize(ctx, width: int, height: int = None):
    if not ctx.message.attachments:
        await ctx.send("Upload foto dulu! 📸")
        return
    try:
        file = ctx.message.attachments[0]
        img_bytes = await file.read()
        img = Image.open(io.BytesIO(img_bytes))
        ow, oh = img.size
        if width > ow:
            await ctx.send(f"❌ Ga bisa upscale! Asli: `{ow}x{oh}`")
            return
        h = height or int(oh * (width / ow))
        img = img.resize((width, h), Image.LANCZOS)
        output = io.BytesIO()
        ext = file.filename.split(".")[-1].lower()
        sf = "JPEG" if ext in ["jpg", "jpeg"] else ext.upper()
        if img.mode in ("RGBA", "P") and sf == "JPEG":
            img = img.convert("RGB")
        img.save(output, format=sf)
        output.seek(0)
        await ctx.reply(f"✅ `{width}x{h}`!", file=discord.File(output, filename=f"resized.{ext}"))
    except Exception as e:
        await ctx.send(f"Gagal: {e}")

@bot.command(help="compress foto", usage="upload foto !compress [quality]")
async def compress(ctx, quality: int = 60):
    if not ctx.message.attachments:
        await ctx.send("Upload foto dulu! 📸")
        return
    if not 1 <= quality <= 95:
        await ctx.send("Quality 1-95")
        return
    try:
        img_bytes = await ctx.message.attachments[0].read()
        img = Image.open(io.BytesIO(img_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=quality, optimize=True)
        output.seek(0)
        await ctx.reply(f"✅ `{len(img_bytes)/1024:.1f}KB` → `{output.getbuffer().nbytes/1024:.1f}KB`",
            file=discord.File(output, filename="compressed.jpg"))
    except Exception as e:
        await ctx.send(f"Gagal: {e}")

bot.remove_command("help")

@bot.command()
async def help(ctx, *, command: str = None):
    if command:
        cmd = bot.get_command(command)
        if not cmd:
            await ctx.send(f"Command `{command}` ga ketemu 😅")
            return
        embed = discord.Embed(title=f"📖 !{cmd.name}", color=0x00ff99)
        embed.add_field(name="Cara pake", value=f"`{cmd.usage or '-'}`", inline=False)
        embed.add_field(name="Deskripsi", value=cmd.help or "-", inline=False)
        await ctx.send(embed=embed)
        return
    embed = discord.Embed(title="📚 Enki Help", description="`!help <command>` untuk detail", color=0x00ff99)
    embed.add_field(name="🤖 AI", value="`chat` `debug` `review` `roast`", inline=False)
    embed.add_field(name="🌤️ Info", value="`cuaca` `forecast` `news` `translate`", inline=False)
    embed.add_field(name="📋 Personal", value="`remind` `todo` `note` `afk`", inline=False)
    embed.add_field(name="🖼️ Foto", value="`convert` `resize` `compress`", inline=False)
    embed.add_field(name="🎮 Game", value="`wack` `leaderboard` `ball`", inline=False)
    embed.add_field(name="📊 Server", value="`serverinfo` `userinfo` `stats` `setprefix` `uptime` `ping` `calc`", inline=False)
    embed.set_footer(text="Enki v1.0 | by Ren Lumireign")
    await ctx.send(embed=embed)

if not TOKEN:
    print("ERROR: TOKEN tidak ditemukan!")
else:
    bot.run(TOKEN)
