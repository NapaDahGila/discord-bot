import os
import json
import discord
from discord.ext import commands
from groq import Groq
from keep_alive import keep_alive

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

            save_memory()  # 🔥 bikin permanen

            if len(reply) > 2000:
                reply = reply[:1990] + "..."

            await ctx.send(reply)

        except Exception as e:
            print("ERROR:", e)
            await ctx.send("AI error 😅")

@bot.event
async def on_message(message):

    # Jangan respon diri sendiri
    if message.author == bot.user:
        return

    # Optional: cuma respon di channel tertentu
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

    await bot.process_commands(message)

keep_alive()
bot.run(TOKEN)
