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
import libsql_experimental as libsql
from datetime import datetime
from discord.ext import commands
from groq import Groq

TOKEN = os.getenv("TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")
WEATHER_KEY = os.getenv("WEATHER_KEY")
NEWS_KEY = os.getenv("NEWS_KEY")
TURSO_URL = os.getenv("TURSO_URL")
TURSO_TOKEN = os.getenv("TURSO_TOKEN")

client = Groq(api_key=GROQ_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

START_TIME = time.time()

# ===== DATABASE (Turso) =====

_db_conn = None
_prefix_cache = {}

def get_db():
    global _db_conn
    local_file = "/tmp/memory.db"  # /tmp lebih stabil di Railway, tidak hilang saat redeploy

    if _db_conn is not None:
        try:
            _db_conn.execute("SELECT 1")
            return _db_conn
        except Exception as e:
            print(f"[DB] Koneksi lama error: {e} → reconnect...")
            _db_conn = None

    try:
        print(f"[DB] Membuat koneksi baru ke Turso...")
        print(f"[DB] sync_url: {TURSO_URL}")
        print(f"[DB] auth_token: {'<ada>' if TURSO_TOKEN and len(TURSO_TOKEN) > 20 else '<kosong/invalid>'}")

        if not TURSO_URL or not TURSO_URL.startswith("libsql://"):
            raise ValueError("TURSO_URL salah! Harus dimulai dengan 'libsql://'")

        if not TURSO_TOKEN:
            raise ValueError("TURSO_TOKEN kosong!")

        _db_conn = libsql.connect(
            local_file,
            sync_url=TURSO_URL,
            auth_token=TURSO_TOKEN
        )

        print("[DB] Koneksi dibuat, mulai sync (pull dari Turso)...")
        _db_conn.sync()
        print("[DB] Sync awal selesai")

        # Log tabel yang berhasil di-pull
        tables = _db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        print(f"[DB] Tabel dari Turso: {[t[0] for t in tables]}")

        # Log prefixes yang berhasil di-pull
        try:
            prefixes = _db_conn.execute("SELECT * FROM prefixes").fetchall()
            print(f"[DB] Prefixes dari Turso: {prefixes}")
        except Exception:
            print("[DB] Tabel prefixes belum ada di Turso (normal kalau fresh)")

        # Verifikasi SQLite version
        rows = _db_conn.execute("SELECT sqlite_version()").fetchone()
        print(f"[DB] SQLite version: {rows[0]}")

        return _db_conn

    except Exception as e:
        print(f"[DB] GAGAL connect/sync: {type(e).__name__} → {str(e)}")
        if os.path.exists(local_file):
            try:
                os.remove(local_file)
                print(f"[DB] File lokal dihapus karena kemungkinan corrupt")
            except Exception as rm_err:
                print(f"[DB] Gagal hapus file lokal: {rm_err}")
        raise


def init_db():
    conn = get_db()
    print("[INIT_DB] Memulai inisialisasi tabel...")

    tables = [
        """CREATE TABLE IF NOT EXISTS memory (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
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
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            channel_id  TEXT NOT NULL,
            pesan       TEXT NOT NULL,
            waktu       REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS todos (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   TEXT NOT NULL,
            tugas     TEXT NOT NULL,
            selesai   INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS notes (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   TEXT NOT NULL,
            judul     TEXT NOT NULL,
            isi       TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS user_profiles (
            user_id     TEXT PRIMARY KEY,
            nickname    TEXT,
            preferences TEXT DEFAULT '{}'
        )""",
        """CREATE TABLE IF NOT EXISTS discord_users (
            user_id         TEXT PRIMARY KEY,
            username        TEXT NOT NULL,
            display_name    TEXT,
            joined_at       TEXT,
            account_created TEXT,
            roles           TEXT DEFAULT '[]',
            message_count   INTEGER DEFAULT 0,
            last_seen       TEXT,
            guild_id        TEXT
        )""",
    ]

    try:
        for i, sql in enumerate(tables, 1):
            table_name = sql.split("TABLE IF NOT EXISTS")[1].split("(")[0].strip()
            print(f"  [{i}/{len(tables)}] Membuat/mengecek tabel: {table_name}")
            conn.execute(sql)

        print("[INIT_DB] Semua CREATE TABLE selesai, mulai sync ke Turso...")
        db_sync(conn)
        print("[INIT_DB] Sync #1 selesai")
        db_sync(conn)
        print("[INIT_DB] Sync #2 selesai → tabel sudah di Turso")

        existing_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        print(f"[INIT_DB] Tabel yang ada: {[t[0] for t in existing_tables]}")

        # Log isi prefixes setelah init — buat verifikasi data persist
        prefixes = conn.execute("SELECT * FROM prefixes").fetchall()
        print(f"[INIT_DB] Prefixes saat ini: {prefixes}")

    except Exception as e:
        print(f"[INIT_DB] ERROR: {type(e).__name__} → {str(e)}")
        raise


# ===== MEMORY =====

def load_memory(user_id: str, limit: int = 15) -> list:
    conn = get_db()
    rows = conn.execute("""
        SELECT role, content FROM (
            SELECT id, role, content FROM memory
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        ) ORDER BY id ASC
    """, (user_id, limit)).fetchall()
    return [{"role": r, "content": c} for r, c in rows]


def db_sync(conn):
    """Commit lokal dulu baru push ke Turso — wajib dipanggil setelah setiap write."""
    conn.commit()
    conn.sync()


def save_message(user_id: str, role: str, content: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO memory (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content)
    )
    conn.execute("""
        DELETE FROM memory WHERE user_id = ? AND id NOT IN (
            SELECT id FROM memory WHERE user_id = ? ORDER BY id DESC LIMIT 15
        )
    """, (user_id, user_id))
    db_sync(conn)


def reset_memory(user_id: str):
    conn = get_db()
    conn.execute("DELETE FROM memory WHERE user_id = ?", (user_id,))
    db_sync(conn)


# ===== PROFILE =====

def get_profile(user_id: str) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT nickname, preferences FROM user_profiles WHERE user_id = ?",
        (user_id,)
    ).fetchone()
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
    current = get_profile(user_id)
    new_nickname = nickname if nickname is not None else current["nickname"]
    new_prefs = preferences if preferences is not None else current["preferences"]
    conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
    conn.execute(
        "INSERT INTO user_profiles (user_id, nickname, preferences) VALUES (?, ?, ?)",
        (user_id, new_nickname, json.dumps(new_prefs))
    )
    db_sync(conn)
    print(f"[PROFILE] saved user_id={user_id} nickname={new_nickname}")


# ===== DISCORD USERS =====

def upsert_discord_user(member: discord.Member, increment_message: bool = False):
    """Simpan/update data user Discord. TIDAK sync ke Turso — sync dilakukan terpisah."""
    try:
        conn = get_db()
        user_id = str(member.id)
        username = member.name
        display_name = member.display_name
        joined_at = member.joined_at.isoformat() if member.joined_at else None
        account_created = member.created_at.isoformat()
        roles = json.dumps([r.name for r in member.roles[1:]])
        last_seen = datetime.now(pytz.timezone("Asia/Jakarta")).isoformat()
        guild_id = str(member.guild.id)

        existing = conn.execute(
            "SELECT message_count FROM discord_users WHERE user_id = ?", (user_id,)
        ).fetchone()

        if existing:
            new_count = (existing[0] or 0) + (1 if increment_message else 0)
            conn.execute("""
                UPDATE discord_users SET
                    username = ?,
                    display_name = ?,
                    joined_at = ?,
                    account_created = ?,
                    roles = ?,
                    message_count = ?,
                    last_seen = ?
                WHERE user_id = ?
            """, (username, display_name, joined_at, account_created, roles, new_count, last_seen, user_id))
        else:
            conn.execute("""
                INSERT INTO discord_users
                    (user_id, username, display_name, joined_at, account_created, roles, message_count, last_seen, guild_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, username, display_name, joined_at, account_created, roles,
                  1 if increment_message else 0, last_seen, guild_id))
        # Tidak sync di sini — sync dilakukan batch oleh caller atau periodic_sync
    except Exception as e:
        print(f"[DISCORD_USER] Error upsert {member.name}: {e}")


# ===== PREFIX =====

def get_prefix(bot, message):
    if not message.guild:
        return "!"
    guild_id = str(message.guild.id)
    if guild_id in _prefix_cache:
        return _prefix_cache[guild_id]
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT prefix FROM prefixes WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        prefix = row[0] if row else "!"
        _prefix_cache[guild_id] = prefix
        print(f"[PREFIX] Load dari DB: guild={guild_id} prefix={prefix}")
        return prefix
    except Exception as e:
        print(f"[PREFIX] ERROR get_prefix: {e}")
        return "!"


def set_prefix(guild_id: str, prefix: str):
    conn = get_db()
    conn.execute("""
        INSERT INTO prefixes (guild_id, prefix) VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET prefix = ?
    """, (guild_id, prefix, prefix))

    row = conn.execute(
        "SELECT prefix FROM prefixes WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    print(f"[PREFIX] Sebelum sync: {row}")

    try:
        db_sync(conn)  # commit() dulu baru sync() ke Turso
        print(f"[PREFIX] Sync ke Turso berhasil")
    except Exception as e:
        print(f"[PREFIX] Sync GAGAL: {e}")

    row2 = conn.execute(
        "SELECT prefix FROM prefixes WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    print(f"[PREFIX] Sesudah sync: {row2}")

    _prefix_cache[guild_id] = prefix
    print(f"[PREFIX] Cache diupdate: guild={guild_id} prefix={prefix}")


# ===== WACK SCORES =====

def save_wack_score(user_id: str, username: str, skor: int, total: int):
    conn = get_db()
    conn.execute("""
        INSERT INTO wack_scores (user_id, username, best, total, games)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            username = ?,
            best = MAX(best, ?),
            total = total + ?,
            games = games + 1
    """, (user_id, username, skor, skor, username, skor, skor))
    db_sync(conn)


def get_leaderboard():
    conn = get_db()
    return conn.execute(
        "SELECT username, best, total, games FROM wack_scores ORDER BY best DESC LIMIT 10"
    ).fetchall()


# ===== REMINDER =====

async def cek_reminder():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            sekarang = time.time()
            conn = get_db()
            rows = conn.execute(
                "SELECT id, user_id, channel_id, pesan FROM reminders WHERE waktu <= ?",
                (sekarang,)
            ).fetchall()
            for row in rows:
                id, user_id, channel_id, pesan = row
                channel = bot.get_channel(int(channel_id))
                if channel:
                    await channel.send(f"⏰ <@{user_id}> Reminder: **{pesan}**")
                conn.execute("DELETE FROM reminders WHERE id = ?", (id,))
            if rows:
                db_sync(conn)
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


async def periodic_sync():
    """Push perubahan discord_users ke Turso setiap 60 detik — non-blocking."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            conn = get_db()
            await asyncio.get_event_loop().run_in_executor(None, lambda: db_sync(conn))
            print("[SYNC] Periodic sync ke Turso selesai")
        except Exception as e:
            print(f"[SYNC] Periodic sync error: {e}")
        await asyncio.sleep(60)


# ===== INIT =====

init_db()
afk_users = {}
active_channels = {}

bot = commands.Bot(command_prefix=get_prefix, intents=intents)


# ===== HELPERS =====

def is_wake_call(text):
    keywords = ["wake up enki", "enki bangun", "hey enki", "hei enki"]
    return any(k in text for k in keywords)


def strip_thinking(text: str) -> str:
    import re
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    return text.strip()


def is_creator_question(text):
    keywords = ["dibuat siapa", "desain siapa", "siapa yang buat"]
    return any(k in text for k in keywords)


# ===== INTENT PROCESSOR =====

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
                conn = get_db()
                conn.execute(
                    "INSERT INTO todos (user_id, tugas) VALUES (?, ?)", (user_id, value)
                )
                db_sync(conn)
            except Exception as db_err:
                print(f"[INTENT] todo_add ERROR: {db_err}")
                reply = "Gagal simpan todo 😅"

        elif intent == "todo_list":
            try:
                conn = get_db()
                rows = conn.execute(
                    "SELECT id, tugas, selesai FROM todos WHERE user_id = ?", (user_id,)
                ).fetchall()
                if rows:
                    items = "\n".join([("✅" if s else "⬜") + f" #{i} {t}" for i, t, s in rows])
                    reply = "📋 Todo list kamu:\n" + items
                else:
                    reply = "Todo list kamu kosong 😴"
            except Exception as db_err:
                print(f"[INTENT] todo_list ERROR: {db_err}")
                reply = "Gagal baca todo 😅"

        elif intent == "todo_done":
            try:
                conn = get_db()
                conn.execute(
                    "UPDATE todos SET selesai = 1 WHERE id = ? AND user_id = ?", (value, user_id)
                )
                db_sync(conn)
            except Exception as db_err:
                print(f"[INTENT] todo_done ERROR: {db_err}")
                reply = "Gagal update todo 😅"

        elif intent == "note_add":
            parts = value.split("|", 1)
            if len(parts) == 2:
                try:
                    conn = get_db()
                    conn.execute(
                        "INSERT INTO notes (user_id, judul, isi) VALUES (?, ?, ?)",
                        (user_id, parts[0].strip(), parts[1].strip())
                    )
                    db_sync(conn)
                except Exception as db_err:
                    print(f"[INTENT] note_add ERROR: {db_err}")
                    reply = "Gagal simpan catatan 😅"

        elif intent == "remind_add":
            parts = value.split("|", 1)
            if len(parts) == 2:
                waktu_str, pesan = parts[0].strip(), parts[1].strip()
                satuan = waktu_str[-1]
                try:
                    angka = int(waktu_str[:-1])
                    detik = angka * (1 if satuan == "s" else 60 if satuan == "m" else 3600)
                    conn = get_db()
                    conn.execute(
                        "INSERT INTO reminders (user_id, channel_id, pesan, waktu) VALUES (?, ?, ?, ?)",
                        (user_id, str(message.channel.id), pesan, time.time() + detik)
                    )
                    db_sync(conn)
                except Exception as db_err:
                    print(f"[INTENT] remind_add ERROR: {db_err}")
                    reply = "Gagal set reminder 😅"

        elif intent == "todo_delete":
            try:
                conn = get_db()
                conn.execute(
                    "DELETE FROM todos WHERE id = ? AND user_id = ?", (value, user_id)
                )
                db_sync(conn)
            except Exception as db_err:
                print(f"[INTENT] todo_delete ERROR: {db_err}")
                reply = "Gagal hapus todo 😅"

        elif intent == "note_list":
            try:
                conn = get_db()
                rows = conn.execute(
                    "SELECT id, judul FROM notes WHERE user_id = ?", (user_id,)
                ).fetchall()
                if rows:
                    items = "\n".join([f"📝 #{i} {j}" for i, j in rows])
                    reply = "📒 Catatan lo:\n" + items
                else:
                    reply = "Belum ada catatan 😴"
            except Exception as db_err:
                print(f"[INTENT] note_list ERROR: {db_err}")
                reply = "Gagal baca catatan 😅"

        elif intent == "note_get":
            try:
                conn = get_db()
                row = conn.execute(
                    "SELECT judul, isi FROM notes WHERE id = ? AND user_id = ?", (value, user_id)
                ).fetchone()
                if row:
                    judul, isi = row
                    reply = f"📝 **{judul}**\n{isi}"
                else:
                    reply = "Catatan ga ketemu 😅"
            except Exception as db_err:
                print(f"[INTENT] note_get ERROR: {db_err}")
                reply = "Gagal baca catatan 😅"

        elif intent == "note_delete":
            try:
                conn = get_db()
                conn.execute(
                    "DELETE FROM notes WHERE id = ? AND user_id = ?", (value, user_id)
                )
                db_sync(conn)
            except Exception as db_err:
                print(f"[INTENT] note_delete ERROR: {db_err}")
                reply = "Gagal hapus catatan 😅"

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
            except Exception as db_err:
                print(f"[INTENT] profile_update ERROR: {db_err}")
                reply = "Gagal update profil 😅"

        elif intent == "cuaca":
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"http://api.openweathermap.org/data/2.5/weather?q={value}&appid={WEATHER_KEY}&units=metric&lang=id"
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            reply = f"Kota `{value}` ga ketemu 😅"
                        else:
                            d = await resp.json()
                            desc = d["weather"][0]["description"]
                            suhu = d["main"]["temp"]
                            kelembaban = d["main"]["humidity"]
                            angin = d["wind"]["speed"]
                            reply = (
                                f"🌤️ Cuaca di **{value.title()}**\n"
                                f"Kondisi: `{desc}`\n"
                                f"🌡️ Suhu: `{suhu}°C` | 💧 Kelembaban: `{kelembaban}%` | 💨 Angin: `{angin} m/s`"
                            )
            except Exception as e:
                print(f"[INTENT] cuaca ERROR: {e}")
                reply = "Gagal ngambil data cuaca 😅"

        elif intent == "forecast":
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"http://api.openweathermap.org/data/2.5/forecast?q={value}&appid={WEATHER_KEY}&units=metric&lang=id&cnt=24"
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            reply = f"Kota `{value}` ga ketemu 😅"
                        else:
                            d = await resp.json()
                            hari = {}
                            for item in d["list"]:
                                tanggal = item["dt_txt"].split(" ")[0]
                                if tanggal not in hari:
                                    hari[tanggal] = {
                                        "desc": item["weather"][0]["description"],
                                        "min": item["main"]["temp_min"],
                                        "max": item["main"]["temp_max"],
                                    }
                                else:
                                    hari[tanggal]["min"] = min(hari[tanggal]["min"], item["main"]["temp_min"])
                                    hari[tanggal]["max"] = max(hari[tanggal]["max"], item["main"]["temp_max"])
                            lines = [f"🌤️ Forecast **{value.title()}**"]
                            for tanggal, info in list(hari.items())[:4]:
                                lines.append(
                                    f"📅 {tanggal}: `{info['desc']}` {info['min']:.1f}°C - {info['max']:.1f}°C"
                                )
                            reply = "\n".join(lines)
            except Exception as e:
                print(f"[INTENT] forecast ERROR: {e}")
                reply = "Gagal ngambil forecast 😅"

        elif intent == "news":
            try:
                topik = value if value else "indonesia"
                async with aiohttp.ClientSession() as session:
                    url = f"https://newsapi.org/v2/everything?q={topik}&language=id&sortBy=publishedAt&pageSize=5&apiKey={NEWS_KEY}"
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            reply = "Gagal ngambil berita 😅"
                        else:
                            d = await resp.json()
                            articles = d.get("articles", [])
                            if not articles:
                                reply = f"Ga ada berita tentang `{topik}` 😅"
                            else:
                                lines = [f"📰 Berita terkini: **{topik.title()}**"]
                                for a in articles[:5]:
                                    lines.append(
                                        f"📌 [{a['title']}]({a['url']}) — _{a['source']['name']}_"
                                    )
                                reply = "\n".join(lines)
            except Exception as e:
                print(f"[INTENT] news ERROR: {e}")
                reply = "Gagal ngambil berita 😅"

        elif intent == "translate":
            try:
                parts = value.split("|", 1)
                if len(parts) == 2:
                    bahasa, teks = parts[0].strip(), parts[1].strip()
                    async with aiohttp.ClientSession() as session:
                        url = f"https://api.mymemory.translated.net/get?q={teks}&langpair=id|{bahasa}"
                        async with session.get(url) as resp:
                            if resp.status != 200:
                                reply = "Gagal translate 😅"
                            else:
                                d = await resp.json()
                                hasil = d["responseData"]["translatedText"]
                                reply = f"🌐 **Translate** (id → {bahasa})\n`{teks}` → `{hasil}`"
                else:
                    reply = "Format translate salah. Contoh: 'translate ke en teks kamu'"
            except Exception as e:
                print(f"[INTENT] translate ERROR: {e}")
                reply = "Gagal translate 😅"

        if reply:
            embed = discord.Embed(description=reply, color=0x5865F2)
            embed.set_footer(
                text=f"Enki • {datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%H:%M')}"
            )
            await message.channel.send(embed=embed)

    except Exception as e:
        print(f"[INTENT] OUTER ERROR: {e} | raw: {repr(reply_text[:100])}")
        import re
        m = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)"', reply_text)
        if m:
            await message.channel.send(m.group(1))
        else:
            await message.channel.send("Hmm, gw lagi error dikit 😅 Coba lagi?")


# ===== EVENTS =====

@bot.event
async def on_ready():
    print(f"[READY] Bot online sebagai {bot.user}")
    try:
        init_db()
    except Exception as e:
        print(f"[READY] init_db error: {e}")

    asyncio.ensure_future(cek_reminder())
    asyncio.ensure_future(periodic_sync())


@bot.event
async def on_member_join(member: discord.Member):
    upsert_discord_user(member)
    try:
        db_sync(get_db())
    except Exception as e:
        print(f"[JOIN] Gagal sync: {e}")
    print(f"[JOIN] {member.name} join → disimpen ke Turso")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Simpan/update data user Discord setiap pesan
    if message.guild and isinstance(message.author, discord.Member):
        upsert_discord_user(message.author, increment_message=True)

    # Cek AFK mentions
    if message.mentions:
        for user in message.mentions:
            if user.id in afk_users:
                await message.channel.send(
                    f"⚠️ {user.display_name} lagi AFK: `{afk_users[user.id]}`"
                )

    # Welcome back dari AFK
    if message.author.id in afk_users:
        del afk_users[message.author.id]
        embed = discord.Embed(
            description=f"Welcome back {message.author.display_name}! AFK kamu udah dihapus 👋",
            color=0x00ff99
        )
        await message.channel.send(embed=embed)

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
        user_id_reset = str(message.author.id)
        reset_memory(user_id_reset)
        embed = discord.Embed(
            description="🧹 Percakapan kita udah direset. Mulai dari awal!",
            color=0x00ff99
        )
        await message.channel.send(embed=embed)
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
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Lo adalah Enki, asisten pribadi yang cerdas dan efisien — kayak Jarvis-nya Tony Stark. "
                            "Lo ngomong sopan tapi ga kaku, to the point, dan sesekali nyindir halus kalau situasinya pas. "
                            "Jangan basa-basi panjang, langsung jawab intinya. "
                            "PENTING: Deteksi bahasa yang dipakai user, lalu balas SELALU pake bahasa yang sama. "
                            "Kalau user pake bahasa Indonesia -> balas Indonesia. Kalau English -> balas English. Dst. "
                            "Kalau ditanya siapa yang bikin lo: jawab sesuai bahasa user. "
                            "Jangan sebut OpenAI atau model apapun. "
                            f"DATA USER (selalu gunakan ini, jangan abaikan): {profile_info}"
                            "WAJIB: Panggil user sesuai nama panggilan di DATA USER di atas, bukan username Discord. "
                            f"Waktu WIB: {sekarang}. "
                            "WAJIB: Selalu jawab HANYA dengan JSON format ini, tanpa teks lain: "
                            '{"intent":"...","data":"...","reply":"..."} '
                            "Intent tersedia: "
                            "todo_add(data=tugas), todo_list(data=), todo_done(data=id), todo_delete(data=id), "
                            "note_add(data=judul|isi), note_list(data=), note_get(data=id), note_delete(data=id), "
                            "remind_add(data=10m|pesan), "
                            "cuaca(data=nama_kota), forecast(data=nama_kota), "
                            "news(data=topik_opsional), translate(data=en|teks), "
                            "profile_update(data=nickname:nama|key:value), chat(data=) "
                            "Contoh-contoh: "
                            'user: tambahin todo belajar python -> {"intent":"todo_add","data":"belajar python","reply":"Sip, gw tambahin!"} '
                            'user: hapus todo 2 -> {"intent":"todo_delete","data":"2","reply":"Oke dihapus!"} '
                            'user: tampilin semua catatan -> {"intent":"note_list","data":"","reply":"Nih catatan lo!"} '
                            'user: liat catatan 1 -> {"intent":"note_get","data":"1","reply":"Nih isinya!"} '
                            'user: hapus catatan 3 -> {"intent":"note_delete","data":"3","reply":"Oke dihapus!"} '
                            'user: cuaca jakarta -> {"intent":"cuaca","data":"jakarta","reply":"Gw cek dulu!"} '
                            'user: halo -> {"intent":"chat","data":"","reply":"Halo bro!"} '
                            'user: panggil gw Ren -> {"intent":"profile_update","data":"nickname:Ren","reply":"Sip, gw panggil lo Ren!"} '
                            'user: gw suka musik jazz -> {"intent":"profile_update","data":"musik:jazz","reply":"Noted, lo suka jazz!"} '
                            "PENTING: profile_update HANYA boleh dipanggil kalau user EKSPLISIT minta ubah nama panggilan atau kasih tau preferensi. "
                            "Jangan pernah profile_update hanya karena user menyapa atau menyebut nama mereka sendiri. "
                            "Balas dengan bahasa santai gaul, singkat, kayak temen — jangan kaku atau robot."
                        )
                    }
                ] + history + [{"role": "user", "content": message.content}]
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


# ===== COMMANDS =====

@bot.command()
async def ping(ctx):
    await ctx.send("Pong 🏓")


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
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Lo adalah Enki, asisten pribadi yang cerdas dan efisien — kayak Jarvis-nya Tony Stark. "
                            "Lo ngomong sopan tapi ga kaku, to the point, dan sesekali nyindir halus kalau situasinya pas. "
                            "Jangan basa-basi panjang, langsung jawab intinya. "
                            "PENTING: Deteksi bahasa yang dipakai user, lalu balas SELALU pake bahasa yang sama. "
                            "Kalau user pake bahasa Indonesia -> balas Indonesia. Kalau English -> balas English. Dst. "
                            "Kalau ditanya siapa yang bikin lo: jawab sesuai bahasa user. "
                            "Jangan sebut OpenAI atau model apapun. "
                            f"Waktu WIB: {sekarang}."
                        )
                    }
                ] + history + [{"role": "user", "content": message}]
            )

            reply = strip_thinking(response.choices[0].message.content or "AI gak ngasih respon 😅")
            save_message(user_id, "assistant", reply)

            if len(reply) > 2000:
                reply = reply[:1990] + "..."

            embed = discord.Embed(description=reply, color=0x5865F2)
            embed.set_footer(
                text=f"Enki • {datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%H:%M')}"
            )
            await ctx.send(embed=embed)

        except Exception as e:
            print("ERROR:", e)
            await ctx.send("AI error 😅")


@bot.command(help="buat bantu benerin kode lu", usage="upload kode lu terus !debug")
async def debug(ctx, *, question: str = None):
    if not ctx.message.attachments:
        await ctx.send("Upload file Python dulu 🔥")
        return

    file = ctx.message.attachments[0]
    if not file.filename.endswith(".py"):
        await ctx.send("Cuma bisa debug file `.py`")
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

    user_prompt = f"Debug this Python code:\n\n```python\n{code}\n```"
    if question:
        user_prompt += f"\n\nFokus ke masalah ini: {question}"

    async with ctx.typing():
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an expert Python debugger. "
                            "Identify bugs clearly, explain why it's a bug, "
                            "and provide the fixed code with short explanation."
                        )
                    },
                    {"role": "user", "content": user_prompt}
                ]
            )
            reply = strip_thinking(response.choices[0].message.content or "")
            if len(reply) > 2000:
                file_output = io.BytesIO(reply.encode("utf-8"))
                await ctx.reply(
                    "Hasil debug terlalu panjang, nih filenya 📄",
                    file=discord.File(file_output, filename="debug_result.txt")
                )
            else:
                await ctx.reply(reply)
        except Exception as e:
            await ctx.reply(f"AI error: {e}")


@bot.command(help="buat roasting kode lu", usage="upload kode lu, terus !roast")
async def roast(ctx):
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
                model="llama-3.3-70b-versatile",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a savage but funny code roaster. "
                            "Roast this code brutally but keep it humorous. "
                            "Point out bad practices, ugly code, and amateur mistakes "
                            "in a funny way. Be mean but still educational. "
                            "balas pakai bahasa indonesia, ga harus sopan."
                        )
                    },
                    {"role": "user", "content": f"Roast this code:\n\n```python\n{code}\n```"}
                ]
            )
            reply = strip_thinking(response.choices[0].message.content or "")
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


@bot.command(help="buat review kode lu", usage="upload file terus !review")
async def review(ctx, *, question: str = None):
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
                model="llama-3.3-70b-versatile",
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
                    {"role": "user", "content": user_prompt}
                ]
            )
            reply = strip_thinking(response.choices[0].message.content or "")
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


@bot.command(help="buat nunjukin berapa lama enki nyala", usage="!uptime")
async def uptime(ctx):
    uptime_seconds = int(time.time() - START_TIME)
    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60
    seconds = uptime_seconds % 60

    embed = discord.Embed(
        title="⏱️ Uptime Enki",
        description=f"**{days}h {hours}j {minutes}m {seconds}d**",
        color=0x00ff99
    )
    embed.set_footer(text="Enki v1.0")
    await ctx.send(embed=embed)


@bot.command(help="untuk set prefix", usage="!setprefix <bebas>")
@commands.has_permissions(administrator=True)
async def setprefix(ctx, prefix: str):
    set_prefix(str(ctx.guild.id), prefix)
    embed = discord.Embed(
        title="✅ Prefix Updated",
        description=f"Prefix sekarang: `{prefix}`",
        color=0x00ff99
    )
    await ctx.send(embed=embed)


@bot.command(help="buat nunjukin berapa lama lu pakai bot", usage="!stats")
async def stats(ctx):
    user_id = str(ctx.author.id)
    conn = get_db()

    total = conn.execute(
        "SELECT COUNT(*) FROM memory WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    total_user = conn.execute(
        "SELECT COUNT(*) FROM memory WHERE user_id = ? AND role = 'user'", (user_id,)
    ).fetchone()[0]
    total_ai = conn.execute(
        "SELECT COUNT(*) FROM memory WHERE user_id = ? AND role = 'assistant'", (user_id,)
    ).fetchone()[0]

    embed = discord.Embed(title="📊 Stats Kamu", color=0x00ff99)
    embed.add_field(name="Total Pesan", value=f"`{total}`", inline=True)
    embed.add_field(name="Pesan Kamu", value=f"`{total_user}`", inline=True)
    embed.add_field(name="Balasan Enki", value=f"`{total_ai}`", inline=True)
    embed.set_footer(text=f"Stats untuk {ctx.author.display_name}")
    await ctx.send(embed=embed)


@bot.command(help="Cek cuaca kota tertentu", usage="!cuaca <kota>")
async def cuaca(ctx, *, kota: str):
    if not WEATHER_KEY:
        await ctx.send("API key cuaca belum diset.")
        return

    async with aiohttp.ClientSession() as session:
        url = f"http://api.openweathermap.org/data/2.5/weather?q={kota}&appid={WEATHER_KEY}&units=metric&lang=id"
        async with session.get(url) as resp:
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

    embed = discord.Embed(title=f"🌤️ Cuaca di {kota.title()}", color=0x00ff99)
    embed.add_field(name="Kondisi", value=f"`{cuaca_desc}`", inline=False)
    embed.add_field(name="🌡️ Suhu", value=f"`{suhu}°C`", inline=True)
    embed.add_field(name="🔽 Min", value=f"`{suhu_min}°C`", inline=True)
    embed.add_field(name="🔼 Max", value=f"`{suhu_max}°C`", inline=True)
    embed.add_field(name="💧 Kelembaban", value=f"`{kelembaban}%`", inline=True)
    embed.add_field(name="💨 Angin", value=f"`{angin} m/s`", inline=True)
    await ctx.send(embed=embed)


@bot.command(help="Translate teks ke bahasa lain", usage="!translate <kode_bahasa> <teks>")
async def translate(ctx, bahasa: str, *, teks: str):
    async with aiohttp.ClientSession() as session:
        url = f"https://api.mymemory.translated.net/get?q={teks}&langpair=id|{bahasa}"
        async with session.get(url) as resp:
            if resp.status != 200:
                await ctx.send("Gagal translate 😅")
                return
            data = await resp.json()

    hasil = data["responseData"]["translatedText"]
    embed = discord.Embed(title="🌐 Translate", color=0x00ff99)
    embed.add_field(name="Teks Asli", value=f"`{teks}`", inline=False)
    embed.add_field(name="Hasil", value=f"`{hasil}`", inline=False)
    embed.set_footer(text=f"id → {bahasa}")
    await ctx.send(embed=embed)


@bot.command(help="buat seru seruan", usage="!ball <pertanyaan>")
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
    embed = discord.Embed(title="🎱 8Ball", color=0x00ff99)
    embed.add_field(name="Pertanyaan", value=f"`{pertanyaan}`", inline=False)
    embed.add_field(name="Jawaban", value=hasil, inline=False)
    await ctx.send(embed=embed)


@bot.command(help="buat nunjukin kalo lu afk", usage="!afk <alasan>")
async def afk(ctx, *, alasan: str = "AFK"):
    afk_users[ctx.author.id] = alasan
    embed = discord.Embed(
        title="💤 AFK",
        description=f"{ctx.author.display_name} sekarang AFK: `{alasan}`",
        color=0x00ff99
    )
    await ctx.send(embed=embed)


@bot.command(help="minigame wack", usage="!wack [note: pencet emoji sesuai tikus berada]")
async def wack(ctx):
    skor = 0
    ronde = 0

    await ctx.send("🎮 **Whack-a-Mole dimulai!** Klik reaction 🐭 secepat mungkin!\n3...")
    await asyncio.sleep(1)
    await ctx.send("2...")
    await asyncio.sleep(1)
    await ctx.send("1...")
    await asyncio.sleep(1)

    while True:
        ronde += 1
        posisi = random.randint(0, 4)
        lubang = ["🕳️", "🕳️", "🕳️", "🕳️", "🕳️"]
        lubang[posisi] = "🐭"

        papan = " ".join(lubang)
        pesan = await ctx.send(f"**Ronde {ronde}** | Skor: {skor}\n{papan}")

        reactions = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        for r in reactions:
            await pesan.add_reaction(r)

        def check(reaction, user):
            return (
                user == ctx.author
                and str(reaction.emoji) in reactions
                and reaction.message.id == pesan.id
            )

        try:
            reaction, user = await bot.wait_for("reaction_add", timeout=3.0, check=check)
            if reactions.index(str(reaction.emoji)) == posisi:
                skor += 1
                await ctx.send(f"✅ Bener! Skor: {skor}")
            else:
                await ctx.send(f"❌ Salah! Game over! Tikusnya di {reactions[posisi]}")
                break
        except asyncio.TimeoutError:
            await ctx.send(f"⏱️ Timeout! Game over! Tikusnya di {reactions[posisi]}")
            break

        await asyncio.sleep(1)

    save_wack_score(str(ctx.author.id), ctx.author.display_name, skor, ronde)

    embed = discord.Embed(
        title="🎮 Game Over!",
        description=f"Skor akhir: **{skor}**",
        color=0x00ff99
    )
    if skor >= 20:
        embed.set_footer(text="Gila sih lu 🏆")
    elif skor >= 10:
        embed.set_footer(text="Lumayan! 👍")
    else:
        embed.set_footer(text="Latihan lagi bro 😂")
    await ctx.send(embed=embed)


@bot.command(help="buat nunjukin leaderboard minigame wack", usage="!leaderboard")
async def leaderboard(ctx):
    data = get_leaderboard()
    if not data:
        await ctx.send("Belum ada yang main `!wack` 😅")
        return

    embed = discord.Embed(title="🏆 Leaderboard Whack-a-Mole", color=0x00ff99)
    medals = ["🥇", "🥈", "🥉"]
    for i, (username, best, total, games) in enumerate(data):
        medal = medals[i] if i < 3 else f"`{i+1}.`"
        embed.add_field(
            name=f"{medal} {username}",
            value=f"Best: `{best}` | Total: `{total}` | Games: `{games}`",
            inline=False
        )
    await ctx.send(embed=embed)


@bot.command(help="Set reminder", usage="!remind <waktu> <pesan> | contoh: !remind 10m makan")
async def remind(ctx, waktu: str, *, pesan: str):
    satuan = waktu[-1]
    try:
        angka = int(waktu[:-1])
    except Exception:
        await ctx.send("Format waktu salah! Contoh: `!remind 10m makan siang` atau `!remind 1h tidur`")
        return

    if satuan == "s":
        detik = angka
    elif satuan == "m":
        detik = angka * 60
    elif satuan == "h":
        detik = angka * 3600
    else:
        await ctx.send("Satuan waktu: `s` (detik), `m` (menit), `h` (jam)")
        return

    waktu_remind = time.time() + detik
    conn = get_db()
    conn.execute(
        "INSERT INTO reminders (user_id, channel_id, pesan, waktu) VALUES (?, ?, ?, ?)",
        (str(ctx.author.id), str(ctx.channel.id), pesan, waktu_remind)
    )
    db_sync(conn)

    embed = discord.Embed(
        title="⏰ Reminder Set!",
        description=f"Gw bakal ingetin lo: **{pesan}**",
        color=0x00ff99
    )
    embed.set_footer(text=f"dalam {waktu}")
    await ctx.send(embed=embed)


@bot.command(help="Todo list — add/list/done/delete", usage="!todo <add/list/done/delete> <tugas>")
async def todo(ctx, aksi: str, *, tugas: str = None):
    user_id = str(ctx.author.id)
    conn = get_db()

    if aksi == "add":
        if not tugas:
            await ctx.send("Tugas nya apa? `!todo add belajar python`")
            return
        conn.execute("INSERT INTO todos (user_id, tugas) VALUES (?, ?)", (user_id, tugas))
        db_sync(conn)
        embed = discord.Embed(description=f"✅ Ditambahin: **{tugas}**", color=0x00ff99)
        await ctx.send(embed=embed)

    elif aksi == "list":
        rows = conn.execute(
            "SELECT id, tugas, selesai FROM todos WHERE user_id = ?", (user_id,)
        ).fetchall()
        if not rows:
            await ctx.send("Todo list kamu kosong 😴")
            return
        embed = discord.Embed(title="📋 Todo List", color=0x00ff99)
        for id, tugas, selesai in rows:
            status = "✅" if selesai else "⬜"
            embed.add_field(name=f"{status} #{id}", value=tugas, inline=False)
        await ctx.send(embed=embed)

    elif aksi == "done":
        if not tugas:
            await ctx.send("Masukkin ID tugasnya! `!todo done 1`")
            return
        conn.execute(
            "UPDATE todos SET selesai = 1 WHERE id = ? AND user_id = ?", (tugas, user_id)
        )
        db_sync(conn)
        embed = discord.Embed(description=f"✅ Tugas #{tugas} selesai!", color=0x00ff99)
        await ctx.send(embed=embed)

    elif aksi == "delete":
        if not tugas:
            await ctx.send("Masukkin ID tugasnya! `!todo delete 1`")
            return
        conn.execute("DELETE FROM todos WHERE id = ? AND user_id = ?", (tugas, user_id))
        db_sync(conn)
        embed = discord.Embed(description=f"🗑️ Tugas #{tugas} dihapus!", color=0x00ff99)
        await ctx.send(embed=embed)

    else:
        await ctx.send("Aksi ga valid! Gunain: `add`, `list`, `done`, `delete`")


@bot.command(help="Simpan catatan — add/list/get/delete", usage="!note <add/list/get/delete> <judul | isi>")
async def note(ctx, aksi: str, *, konten: str = None):
    user_id = str(ctx.author.id)
    conn = get_db()

    if aksi == "add":
        if not konten:
            await ctx.send("Format: `!note add judul | isi catatan`")
            return
        if "|" not in konten:
            await ctx.send("Pisahin judul dan isi pake `|` ya! `!note add judul | isi catatan`")
            return
        judul, isi = konten.split("|", 1)
        conn.execute(
            "INSERT INTO notes (user_id, judul, isi) VALUES (?, ?, ?)",
            (user_id, judul.strip(), isi.strip())
        )
        db_sync(conn)
        embed = discord.Embed(
            description=f"📝 Catatan **{judul.strip()}** disimpan!", color=0x00ff99
        )
        await ctx.send(embed=embed)

    elif aksi == "list":
        rows = conn.execute(
            "SELECT id, judul FROM notes WHERE user_id = ?", (user_id,)
        ).fetchall()
        if not rows:
            await ctx.send("Belum ada catatan 😴")
            return
        embed = discord.Embed(title="📒 Catatan Kamu", color=0x00ff99)
        for id, judul in rows:
            embed.add_field(name=f"#{id}", value=judul, inline=False)
        await ctx.send(embed=embed)

    elif aksi == "get":
        if not konten:
            await ctx.send("Masukkin ID catatan! `!note get 1`")
            return
        row = conn.execute(
            "SELECT judul, isi FROM notes WHERE id = ? AND user_id = ?", (konten, user_id)
        ).fetchone()
        if not row:
            await ctx.send("Catatan ga ketemu 😅")
            return
        judul, isi = row
        embed = discord.Embed(title=f"📝 {judul}", description=isi, color=0x00ff99)
        await ctx.send(embed=embed)

    elif aksi == "delete":
        if not konten:
            await ctx.send("Masukkin ID catatan! `!note delete 1`")
            return
        conn.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (konten, user_id))
        db_sync(conn)
        embed = discord.Embed(description=f"🗑️ Catatan #{konten} dihapus!", color=0x00ff99)
        await ctx.send(embed=embed)

    else:
        await ctx.send("Aksi ga valid! Gunain: `add`, `list`, `get`, `delete`")


@bot.command(help="buat nunjukin informasi server", usage="!serverinfo")
async def serverinfo(ctx):
    guild = ctx.guild
    embed = discord.Embed(title=f"📊 Info Server {guild.name}", color=0x00ff99)
    embed.add_field(name="👑 Owner ID", value=f"`{guild.owner_id}`", inline=True)
    embed.add_field(name="👥 Member", value=f"`{guild.member_count}`", inline=True)
    embed.add_field(name="📅 Dibuat", value=guild.created_at.strftime("%d %B %Y"), inline=True)
    embed.add_field(name="💬 Channel", value=f"`{len(guild.channels)}`", inline=True)
    embed.add_field(name="🎭 Roles", value=f"`{len(guild.roles)}`", inline=True)
    embed.add_field(name="😀 Emoji", value=f"`{len(guild.emojis)}`", inline=True)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    await ctx.send(embed=embed)


@bot.command(help="buat nunjukin informasi user", usage="!userinfo")
async def userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    conn = get_db()

    db_data = conn.execute(
        "SELECT message_count, last_seen FROM discord_users WHERE user_id = ?",
        (str(member.id),)
    ).fetchone()

    msg_count = db_data[0] if db_data else 0
    last_seen = db_data[1][:16].replace("T", " ") if db_data and db_data[1] else "Belum tercatat"

    embed = discord.Embed(title=f"👤 Info User {member.display_name}", color=0x00ff99)
    embed.add_field(name="🏷️ Username", value=f"`{member.name}`", inline=True)
    embed.add_field(name="🆔 ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="📅 Akun Dibuat", value=member.created_at.strftime("%d %B %Y"), inline=True)
    embed.add_field(
        name="📥 Join Server",
        value=member.joined_at.strftime("%d %B %Y") if member.joined_at else "?",
        inline=True
    )
    embed.add_field(name="💬 Pesan Tercatat", value=f"`{msg_count}`", inline=True)
    embed.add_field(name="👁️ Terakhir Aktif", value=f"`{last_seen}`", inline=True)
    embed.add_field(
        name="🎭 Roles",
        value=", ".join([r.name for r in member.roles[1:]]) or "Tidak ada",
        inline=False
    )
    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)
    await ctx.send(embed=embed)


@bot.command(help="buat ngeliat cuaca 4 hari kedepan", usage="!forecast <kota>")
async def forecast(ctx, *, kota: str):
    if not WEATHER_KEY:
        await ctx.send("API key cuaca belum diset.")
        return

    async with aiohttp.ClientSession() as session:
        url = f"http://api.openweathermap.org/data/2.5/forecast?q={kota}&appid={WEATHER_KEY}&units=metric&lang=id&cnt=24"
        async with session.get(url) as resp:
            if resp.status != 200:
                await ctx.send(f"Kota `{kota}` ga ketemu 😅")
                return
            data = await resp.json()

    embed = discord.Embed(title=f"🌤️ Forecast {kota.title()} - 4 Hari", color=0x00ff99)
    hari = {}
    for item in data["list"]:
        tanggal = item["dt_txt"].split(" ")[0]
        if tanggal not in hari:
            hari[tanggal] = {
                "desc": item["weather"][0]["description"],
                "suhu_min": item["main"]["temp_min"],
                "suhu_max": item["main"]["temp_max"],
            }
        else:
            hari[tanggal]["suhu_min"] = min(hari[tanggal]["suhu_min"], item["main"]["temp_min"])
            hari[tanggal]["suhu_max"] = max(hari[tanggal]["suhu_max"], item["main"]["temp_max"])

    for tanggal, info in list(hari.items())[:4]:
        embed.add_field(
            name=f"📅 {tanggal}",
            value=f"`{info['desc']}`\n🌡️ {info['suhu_min']:.1f}°C - {info['suhu_max']:.1f}°C",
            inline=False
        )
    await ctx.send(embed=embed)


@bot.command(help="buat kalkulator", usage="!calc 32*12")
async def calc(ctx, *, ekspresi: str):
    try:
        allowed = set("0123456789+-*/(). ")
        if not all(c in allowed for c in ekspresi):
            await ctx.send("❌ Cuma boleh angka dan operator `+ - * / ( )`")
            return
        hasil = eval(ekspresi)
        embed = discord.Embed(title="🧮 Kalkulator", color=0x00ff99)
        embed.add_field(name="Input", value=f"`{ekspresi}`", inline=False)
        embed.add_field(name="Hasil", value=f"`{hasil}`", inline=False)
        await ctx.send(embed=embed)
    except ZeroDivisionError:
        await ctx.send("❌ Ga bisa bagi sama nol 😅")
    except Exception:
        await ctx.send("❌ Ekspresi ga valid!")


@bot.command(help="buat ngecek berita terbaru", usage="!news")
async def news(ctx, *, topik: str = "indonesia"):
    if not NEWS_KEY:
        await ctx.send("API key news belum diset.")
        return

    async with aiohttp.ClientSession() as session:
        url = f"https://newsapi.org/v2/everything?q={topik}&language=id&sortBy=publishedAt&pageSize=5&apiKey={NEWS_KEY}"
        async with session.get(url) as resp:
            if resp.status != 200:
                await ctx.send("Gagal ngambil berita 😅")
                return
            data = await resp.json()

    articles = data.get("articles", [])
    if not articles:
        await ctx.send(f"Ga ada berita tentang `{topik}` 😅")
        return

    embed = discord.Embed(title=f"📰 Berita Terkini: {topik.title()}", color=0x00ff99)
    for article in articles[:5]:
        judul = article["title"]
        sumber = article["source"]["name"]
        url_berita = article["url"]
        embed.add_field(name=f"📌 {sumber}", value=f"[{judul}]({url_berita})", inline=False)
    await ctx.send(embed=embed)


@bot.command(help="buat ngubah jenis foto, contoh jpg->png", usage="upload foto !convert <format>")
async def convert(ctx, format: str):
    if not ctx.message.attachments:
        await ctx.send("Upload foto dulu! 📸")
        return

    file = ctx.message.attachments[0]
    format = format.lower().strip(".")
    allowed = ["jpg", "jpeg", "png", "webp", "bmp", "gif"]
    if format not in allowed:
        await ctx.send(f"Format ga valid! Pilih: `{', '.join(allowed)}`")
        return

    try:
        img_bytes = await file.read()
        img = Image.open(io.BytesIO(img_bytes))
        if format in ["jpg", "jpeg"] and img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        output = io.BytesIO()
        save_format = "JPEG" if format in ["jpg", "jpeg"] else format.upper()
        img.save(output, format=save_format)
        output.seek(0)
        await ctx.reply(
            f"✅ Converted ke `.{format}`!",
            file=discord.File(output, filename=f"result.{format}")
        )
    except Exception as e:
        await ctx.send(f"Gagal convert: {e}")


@bot.command(help="buat resize pixel foto", usage="upload foto terus !resize <width> [height]")
async def resize(ctx, width: int, height: int = None):
    if not ctx.message.attachments:
        await ctx.send("Upload foto dulu! 📸")
        return

    file = ctx.message.attachments[0]
    try:
        img_bytes = await file.read()
        img = Image.open(io.BytesIO(img_bytes))
        orig_w, orig_h = img.size

        if width > orig_w or (height and height > orig_h):
            await ctx.send(f"❌ Ga bisa upscale! Ukuran asli: `{orig_w}x{orig_h}`")
            return

        if not height:
            ratio = width / orig_w
            height = int(orig_h * ratio)

        img = img.resize((width, height), Image.LANCZOS)
        output = io.BytesIO()
        ext = file.filename.split(".")[-1].lower()
        save_format = "JPEG" if ext in ["jpg", "jpeg"] else ext.upper()
        if img.mode in ("RGBA", "P") and save_format == "JPEG":
            img = img.convert("RGB")
        img.save(output, format=save_format)
        output.seek(0)
        await ctx.reply(
            f"✅ Diresize ke `{width}x{height}`!",
            file=discord.File(output, filename=f"resized.{ext}")
        )
    except Exception as e:
        await ctx.send(f"Gagal resize: {e}")


@bot.command(help="buat ngecompress foto", usage="upload foto dulu, terus !compress [quality]")
async def compress(ctx, quality: int = 60):
    if not ctx.message.attachments:
        await ctx.send("Upload foto dulu! 📸")
        return

    if not 1 <= quality <= 95:
        await ctx.send("Quality harus antara `1-95` — makin kecil makin compress 😄")
        return

    file = ctx.message.attachments[0]
    try:
        img_bytes = await file.read()
        img = Image.open(io.BytesIO(img_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=quality, optimize=True)
        output.seek(0)
        original_size = len(img_bytes) / 1024
        compressed_size = output.getbuffer().nbytes / 1024
        await ctx.reply(
            f"✅ Compressed! `{original_size:.1f}KB` → `{compressed_size:.1f}KB`",
            file=discord.File(output, filename="compressed.jpg")
        )
    except Exception as e:
        await ctx.send(f"Gagal compress: {e}")


bot.remove_command("help")


@bot.command()
async def help(ctx, *, command: str = None):
    if command:
        cmd = bot.get_command(command)
        if not cmd:
            await ctx.send(f"Command `{command}` ga ketemu 😅")
            return
        embed = discord.Embed(title=f"📖 !{cmd.name}", color=0x00ff99)
        embed.add_field(name="Cara pake", value=f"`{cmd.usage or 'Lihat deskripsi'}`", inline=False)
        embed.add_field(name="Deskripsi", value=cmd.help or "Ga ada deskripsi", inline=False)
        await ctx.send(embed=embed)
        return

    embed = discord.Embed(
        title="📚 Enki Help",
        description="Ketik `!help <command>` buat detail tiap command",
        color=0x00ff99
    )
    embed.add_field(name="🤖 AI", value="`chat` `debug` `review` `roast`", inline=False)
    embed.add_field(name="🌤️ Info", value="`cuaca` `forecast` `news` `translate`", inline=False)
    embed.add_field(name="📋 Personal", value="`remind` `todo` `note` `afk`", inline=False)
    embed.add_field(name="🖼️ Foto", value="`convert` `resize` `compress`", inline=False)
    embed.add_field(name="🎮 Game", value="`wack` `leaderboard` `ball`", inline=False)
    embed.add_field(
        name="📊 Server",
        value="`serverinfo` `userinfo` `stats` `setprefix` `uptime` `ping` `calc`",
        inline=False
    )
    embed.set_footer(text="Enki v1.0 | dibuat sama Ren Lumireign")
    await ctx.send(embed=embed)


if not TOKEN:
    print("ERROR: TOKEN tidak ditemukan!")
else:
    bot.run(TOKEN)
