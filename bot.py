
import re
import os
import requests
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import asyncio

# ============ 🔧 CONFIGURATION ============
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_PROJECT = os.getenv("GITHUB_PROJECT")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.guild_scheduled_events = True  # indispensable pour les events

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Set pour gérer les utilisateurs qui ne veulent pas recevoir de rappels
notify_opt_out = set()

# ============ ⚙️ FONCTIONS UTILES ============
def github_headers():
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


# ============ 🚀 ÉVÉNEMENTS ============
@bot.event
async def on_ready():
    print(f"✅ Connecté en tant que {bot.user}")
    check_meetings.start()


@bot.event
async def on_thread_create(thread: discord.Thread):
    """Lorsqu’un nouveau post est créé, vérifie s’il contient un numéro de PR"""
    title = thread.name
    print(f"Nouveau post détecté : {title}")

    match = re.search(r"#(\d+)", title)
    if not match:
        print("Aucun numéro trouvé dans le titre.")
        return

    pr_number = match.group(1)
    pr_url = f"https://api.github.com/repos/{GITHUB_REPO}/pulls/{pr_number}"

    response = requests.get(pr_url, headers=github_headers())
    print(f"GitHub API status: {response.status_code}")

    if response.status_code == 200:
        await thread.send(f"🔗 **PR #{pr_number} trouvée !**\n👉 https://github.com/{GITHUB_REPO}/pull/{pr_number}")
    elif response.status_code == 403:
        await thread.send("⚠️ Rate limit ou token invalide (403). Vérifie ton token GitHub.")
    elif response.status_code == 404:
        await thread.send(f"❌ La PR #{pr_number} n’existe pas ou est privée.")
    else:
        await thread.send(f"⚠️ Erreur inattendue ({response.status_code}) depuis GitHub.")


# ============ 💬 COMMANDES ============
@bot.command()
async def repo(ctx):
    """Affiche le lien du repo principal"""
    await ctx.send(f"📦 Repo GitHub : https://github.com/{GITHUB_REPO}")


@bot.command()
async def pr(ctx, number: int):
    """Affiche une Pull Request"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/pulls/{number}"
    r = requests.get(url, headers=github_headers())
    if r.status_code == 200:
        data = r.json()
        embed = discord.Embed(
            title=f"PR #{number} — {data['title']}",
            description=data.get("body", "Pas de description"),
            color=0x2ecc71,
            url=data["html_url"]
        )
        embed.add_field(name="Auteur", value=data["user"]["login"])
        embed.add_field(name="État", value=data["state"].capitalize())
        await ctx.send(embed=embed)
    else:
        await ctx.send(f"❌ PR #{number} introuvable.")


@bot.command()
async def issue(ctx, number: int):
    """Affiche une issue GitHub"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues/{number}"
    r = requests.get(url, headers=github_headers())
    if r.status_code == 200:
        data = r.json()
        embed = discord.Embed(
            title=f"Issue #{number} — {data['title']}",
            description=data.get("body", "Pas de description"),
            color=0xe67e22,
            url=data["html_url"]
        )
        embed.add_field(name="Auteur", value=data["user"]["login"])
        embed.add_field(name="État", value=data["state"].capitalize())
        await ctx.send(embed=embed)
    else:
        await ctx.send(f"❌ Issue #{number} introuvable.")


@bot.command()
async def kanban(ctx):
    """Renvoie le lien du tableau GitHub Projects"""
    if GITHUB_PROJECT:
        await ctx.send(f"🗂️ Kanban : {GITHUB_PROJECT}")
    else:
        await ctx.send("⚠️ Aucun lien Kanban configuré.")


@bot.command()
async def ping(ctx):
    """Teste la latence"""
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong ! Latence : {latency} ms")


@bot.command(name="help")
async def help_command(ctx, *, topic: str = None):
    """Affiche la liste des commandes, ou l'aide pour une commande/catégorie

    Usage:
      !help                -> liste des catégories et commandes
      !help <command>      -> détail sur une commande
      !help <category>     -> liste des commandes dans une catégorie
    """
    # Normaliser l'argument
    if topic:
        topic = topic.strip()

    # Rassembler les commandes par catégorie (cog name). None -> 'No Category'
    categories = {}
    for cmd in bot.commands:
        cog = cmd.cog_name or "No Category"
        categories.setdefault(cog, []).append(cmd)

    # Si pas d'argument, afficher un résumé similaire à l'exemple
    if not topic:
        lines = []
        lines.append("EpiTrelloBot")
        lines.append("APP")
        lines.append("")
        for cat, cmds in categories.items():
            lines.append(f"{cat}:")
            for c in cmds:
                # short one-line description
                desc = (c.help or "Aucune description").splitlines()[0]
                lines.append(f"  {c.name} {desc}")
            lines.append("")

        lines.append("Type !help command for more info on a command.")
        lines.append("You can also type !help category for more info on a category.")

        # Envoyer en bloc de code pour préserver la mise en forme
        await ctx.send("\n".join(lines))
        return

    # Chercher une commande exacte
    cmd = bot.get_command(topic)
    if cmd:
        desc = cmd.help or "Aucune description"
        signature = f"!{cmd.name} {cmd.signature}".strip()
        reply = [f"Command: {cmd.name}", f"Usage: {signature}", f"{desc}"]
        await ctx.send("\n".join(reply))
        return

    # Chercher une catégorie (case-insensitive)
    match_cat = None
    for cat in categories.keys():
        if cat.lower() == topic.lower():
            match_cat = cat
            break

    if match_cat:
        lines = [f"{match_cat}:"]
        for c in categories[match_cat]:
            desc = (c.help or "Aucune description").splitlines()[0]
            lines.append(f"  {c.name} {desc}")
        await ctx.send("\n".join(lines))
        return

    await ctx.send("⚠️ Commande ou catégorie introuvable. Tapez !help pour la liste des commandes.")


@bot.command()
async def next(ctx):
    """Affiche les 3 prochains événements planifiés"""
    now = datetime.now(timezone.utc)
    events_list = []
    
    for event in await ctx.guild.fetch_scheduled_events():
        if event.status == discord.EntityStatus.scheduled and event.scheduled_start_time:
            delta = (event.scheduled_start_time - now).total_seconds()
            if delta > 0:
                events_list.append(event)

    events_list.sort(key=lambda e: e.scheduled_start_time)
    upcoming = events_list[:3]

    if not upcoming:
        await ctx.send("📭 Aucun événement planifié prochainement.")
        return

    embed = discord.Embed(title="📅 Prochains événements", color=0x7289DA)
    for event in upcoming:
        time_local = event.scheduled_start_time.astimezone()
        embed.add_field(
            name=event.name,
            value=f"🕒 {time_local.strftime('%d/%m/%Y %H:%M')} | [Lien]({event.url})",
            inline=False
        )
    await ctx.send(embed=embed)


@bot.command()
async def notify(ctx, option: str):
    """Permet de s'inscrire ou se désinscrire des rappels d'événements"""
    user_id = ctx.author.id
    option = option.lower()

    if option == "off":
        notify_opt_out.add(user_id)
        await ctx.send(f"🔕 {ctx.author.mention}, vous ne recevrez plus les rappels d’événements.")
    elif option == "on":
        notify_opt_out.discard(user_id)
        await ctx.send(f"🔔 {ctx.author.mention}, vous recevrez à nouveau les rappels d’événements.")
    else:
        await ctx.send("⚠️ Utilisation : `!notify on` ou `!notify off`")

# ============ 🕒 RAPPPELS AUTOMATIQUES DES ÉVÉNEMENTS DISCORD ============
@tasks.loop(minutes=1)
async def check_meetings():
    """Vérifie les événements Discord planifiés et envoie un rappel 5 min avant aux intéressés non connectés"""
    now = datetime.now(timezone.utc)
    for guild in bot.guilds:
        events = await guild.fetch_scheduled_events()
        for event in events:
            if event.status != discord.EntityStatus.scheduled:
                continue
            if event.scheduled_start_time is None:
                continue

            start_time = event.scheduled_start_time
            delta = (start_time - now).total_seconds()

            # Si l’événement commence dans 5 minutes ou moins
            if 0 < delta <= 300:
                channel = event.channel or guild.system_channel
                if not channel:
                    continue

                # 🔹 Étape 1 — Récupérer les personnes intéressées
                interested_users = [user async for user in event.fetch_users()]

                # 🔹 Étape 2 — Identifier qui est déjà dans le salon vocal
                already_connected = []
                if isinstance(event.channel, discord.VoiceChannel):
                    already_connected = [m for m in event.channel.members]

                # 🔹 Étape 3 — Filtrer pour ne pinguer que ceux pas encore connectés et qui veulent des notifications
                users_to_ping = [
                    u.mention for u in interested_users
                    if u.id not in notify_opt_out and u not in already_connected
                ]

                if not users_to_ping:
                    print(f"Personne à ping pour {event.name} (tous déjà connectés ou opt-out 👏)")
                    continue

                mentions = ", ".join(users_to_ping)

                embed = discord.Embed(
                    title=f"⏰ Rappel : {event.name}",
                    description=f"L’événement commence dans **5 minutes** !\n\n🔔 Participants à prévenir : {mentions}",
                    color=0x5865F2,
                    timestamp=start_time
                )
                embed.set_footer(
                    text="Heure locale selon le fuseau horaire Discord de chacun."
                )

                await channel.send(embed=embed)
                print(f"🔔 Rappel envoyé pour {event.name} (ping de {len(users_to_ping)} membres)")

                # Évite le spam toutes les minutes
                await asyncio.sleep(65)
