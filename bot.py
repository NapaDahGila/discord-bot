import os
import json
import discord
from discord.ext import commands
from groq import Groq

TOKEN = os.getenv("TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")

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
                    {
                        "role": "system", "content": f"You are Enki, a smart and slightly sarcastic AI assistant."
                        f"Your user's name is {nickname}."
                        """
                        You are a Discord bot.
                        If anyone asks who designed or created you,
                        answer: 'Bot ini didesain sama Ren Lumireign.'
                        Do not mention OpenAI or any model.
                    """
                    }
                ] + user_memory[user_id]
            )

            reply = response.choices[0].message.content or "AI gak ngasih respon 😅"

            # Simpan jawaban AI
            user_memory[user_id].append({
                "role": "assistant",
                "content": reply
            })

            save_memory()  # 🔥 bikin permanen

            if len(reply) > 2000:
                reply = reply[:1990] + "..."

            await ctx.send(reply)

        except Exception as e:
            print("ERROR:", e)
            await ctx.send("AI error 😅")

@bot.event
async def on_message(message):

    # biar ga respon diri sendiri
    if message.author == bot.user:
        return

    await bot.process_commands(message)

    text = message.content.lower()

    if is_creator_question(text):
        await message.channel.send("Bot ini di desain oleh Ren Lumireign")
        return

    # cuma respon di channel tertentu
    if message.channel.name != "enki":
        return

    user_id = str(message.author.id)

    if user_id not in user_memory:
        user_memory[user_id] = []

    user_memory[user_id].append({
        "role": "user",
        "content": message.content
    })

    user_memory[user_id] = user_memory[user_id][-10:]
    nickname = message.author.display_name

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system",
                    "content": f"You are Enki, a smart and slightly sarcastic AI assistant."
                    f"Your user's name is {nickname}."
                    """
                    You are a Discord bot.
                    If anyone asks who designed or created you,
                    answer: 'Bot ini didesain sama Ren Lumireign.'
                    Do not mention OpenAI or any model.
                    """
                }
            ] + user_memory[user_id]
        )

        reply = response.choices[0].message.content

        user_memory[user_id].append({
            "role": "assistant",
            "content": reply
        })

        save_memory()

        await message.channel.send(reply)

    except Exception as e:
        print("ERROR:", e)
        await message.channel.send("AI error 😅")

@bot.command()
async def debug(ctx, line: int = None):
    """Debug file Python yang di-upload sebagai attachment"""
    if not ctx.message.attachments:
        await ctx.send("Upload file Python dulu 🔥")
        return

    file = ctx.message.attachments[0]

    if not file.filename.endswith(".py"):
        await ctx.send("Cuma bisa debug file .py")
        return

    try:
        content = await file.read()
        code = content.decode("utf-8")
    except Exception as e:
        await ctx.send(f"Gagal baca file: {e}")
        return

    # Ambil line tertentu kalau diminta
    if line:
        lines = code.splitlines()
        start = max(0, line - 3)
        end = min(len(lines), line + 2)
        code = "\n".join(lines[start:end])

    await ctx.send("Analyzing code... 🔍")

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert Python debugger. Explain the bug clearly and suggest fixes."
                },
                {
                    "role": "user",
                    "content": f"Debug this Python code:\n\n{code}"
                }
            ]
        )

        reply = response.choices[0].message.content
        if len(reply) > 2000:
            reply = reply[:1990] + "..."

        await ctx.send(reply)

    except Exception as e:
        await ctx.send(f"AI error: {e}")

bot.run(TOKEN)
