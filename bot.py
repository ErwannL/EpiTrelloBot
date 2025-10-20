
import re
import os
import requests
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Connecté en tant que {bot.user}")

@bot.event
async def on_thread_create(thread: discord.Thread):
    title = thread.name
    print(f"Nouveau post détecté : {title}")

    match = re.search(r"#(\d+)", title)
    if not match:
        print("Aucun numéro trouvé dans le titre.")
        return

    pr_number = match.group(1)
    pr_url = f"https://api.github.com/repos/{GITHUB_REPO}/pulls/{pr_number}"

    headers = {
        "Accept": "application/vnd.github+json",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    response = requests.get(pr_url, headers=headers)
    print(f"GitHub API status: {response.status_code}")

    if response.status_code == 200:
        await thread.send(f"🔗 **PR #{pr_number} trouvée !**\n👉 https://github.com/{GITHUB_REPO}/pull/{pr_number}")
    elif response.status_code == 403:
        await thread.send("⚠️ Rate limit ou token invalide (403). Vérifie ton token GitHub.")
    elif response.status_code == 404:
        await thread.send(f"❌ La PR #{pr_number} n’existe pas ou est privée.")
    else:
        await thread.send(f"⚠️ Erreur inattendue ({response.status_code}) depuis GitHub.")

bot.run(TOKEN)
