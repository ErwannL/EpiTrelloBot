
import re
import os
import requests
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import asyncio
import json
import pytz

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


def get_event_start_time(event):
    """Return a datetime for the event start, handling attribute name differences across discord.py versions."""
    # discord.py renamed/changed scheduled event attributes across versions
    for attr in ("scheduled_start_time", "start_time", "scheduled_start_at"):
        val = getattr(event, attr, None)
        if val is not None:
            return val
    return None


# ============ 🚀 ÉVÉNEMENTS ============
@bot.event
async def on_ready():
    print(f"✅ Connecté en tant que {bot.user}")
    check_meetings.start()
    await check_old_closed_threads()

async def check_old_closed_threads():
    """Parcourt tous les threads fermés mais non archivés pour planifier leur archivage."""
    print("🔍 Vérification des anciens posts fermés...")
    now = datetime.now(timezone.utc)

    for guild in bot.guilds:
        for channel in guild.channels:
            # On ne s’intéresse qu’aux forums
            if isinstance(channel, discord.ForumChannel):
                try:
                    threads = channel.threads
                    if not threads:
                        continue

                    for thread in threads:
                        # Ignorer ceux déjà archivés
                        if thread.archived:
                            continue

                        # Si fermé, planifier l’archivage
                        if thread.locked:
                            # Discord ne donne pas directement la date de fermeture, donc on suppose "fermé récemment"
                            print(f"🧵 Thread fermé détecté : {thread.name}")
                            await thread.send("📦 Ce post est déjà fermé — il sera archivé automatiquement dans 24 heures.")
                            await asyncio.create_task(schedule_archive(thread))
                except Exception as e:
                    print(f"⚠️ Erreur lors de la vérification du forum {channel.name}: {e}")

async def schedule_archive(thread: discord.Thread):
    """Programme l’archivage d’un thread 24h après sa fermeture."""
    try:
        await asyncio.sleep(86400)  # 24 heures
        refreshed = await thread.guild.fetch_channel(thread.id)
        if not refreshed.archived:
            await refreshed.edit(archived=True)
            await refreshed.send("📦 Ce post a été **archivé automatiquement** après 24 heures.")
            print(f"✅ Post '{refreshed.name}' archivé automatiquement après 24h.")
    except Exception as e:
        print(f"⚠️ Erreur dans schedule_archive : {e}")

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

    headers = {
        "Accept": "application/vnd.github+json",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    try:
        response = requests.get(pr_url, headers=headers, timeout=10)
    except requests.RequestException as exc:
        print(f"GitHub API request failed for PR {pr_number}: {exc}")
        await thread.send("⚠️ Erreur lors de la requête vers GitHub pour vérifier la PR. Réessaie plus tard.")
        return

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
    try:
        r = requests.get(url, headers=github_headers(), timeout=10)
    except requests.RequestException as exc:
        print(f"GitHub PR request failed: {exc}")
        await ctx.send("⚠️ Erreur lors de la requête vers GitHub. Réessaie plus tard.")
        return

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
    try:
        r = requests.get(url, headers=github_headers(), timeout=10)
    except requests.RequestException as exc:
        print(f"GitHub issue request failed: {exc}")
        await ctx.send("⚠️ Erreur lors de la requête vers GitHub. Réessaie plus tard.")
        return

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
    if not GITHUB_PROJECT:
        await ctx.send("⚠️ Aucun lien Kanban configuré.")
        return

    # Accept either a full URL or a path like 'users/antoinefld/projects/3' or 'owner/repo/projects/3'
    if isinstance(GITHUB_PROJECT, str) and GITHUB_PROJECT.startswith("http"):
        url = GITHUB_PROJECT
    else:
        url = f"https://github.com/{GITHUB_PROJECT}"

    try:
        await ctx.send(f"🗂️ Kanban : {url}")
    except discord.HTTPException as exc:
        print(f"Failed to send kanban link: {exc}")
        try:
            await ctx.author.send(f"Je n'ai pas pu envoyer le lien du Kanban dans le canal. Voici le lien : {url}")
        except Exception:
            print("Also failed to DM the user the kanban link.")


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


@bot.command(name="next")
async def next_events(ctx):
    """Affiche les 3 prochains événements planifiés sur Discord, en simulant les récurrences."""
    searching_msg = await ctx.send("🔍 Je cherche les prochains événements…")

    guild = ctx.guild
    events = await guild.fetch_scheduled_events()
    now = datetime.now(timezone.utc)
    upcoming = []

    for e in events:
        start_time = get_event_start_time(e)
        if not start_time or e.status != discord.EventStatus.scheduled:
            continue

        # Ajouter l'événement actuel s'il est à venir
        if start_time > now:
            upcoming.append((e.name, start_time, e.id, guild.id))

        # Détecter les événements récurrents (exemple: Weekly)
        if "weekly" in e.name.lower():
            # Générer les 3 prochaines occurrences (hebdomadaire)
            for i in range(1, 4):
                future_start = start_time + timedelta(weeks=i)
                upcoming.append((e.name, future_start, e.id, guild.id))

    # Trier et garder les 3 prochains
    upcoming = sorted(upcoming, key=lambda x: x[1])[:3]

    if not upcoming:
        await searching_msg.edit(content="📭 Aucun événement à venir.")
        return

    msg = "**🗓️ Prochains événements Discord :**\n"
    for name, start_time, event_id, guild_id in upcoming:
        date_str = start_time.astimezone(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M")
        link = f"https://discord.com/events/{guild_id}/{event_id}"
        msg += f"• **{name}** — {date_str} | [Lien]({link})\n"

    await searching_msg.edit(content=msg)


@bot.command(name="notify")
async def notify(ctx, option: str = None):
    """Permet de s'inscrire ou se désinscrire des rappels d'événements."""
    user_id = ctx.author.id

    # ✅ Création du fichier s’il n’existe pas
    if not os.path.exists("notified_users.json"):
        with open("notified_users.json", "w") as f:
            json.dump([], f)

    with open("notified_users.json", "r") as f:
        notified_users = json.load(f)

    # ✅ Cas 1 : !notify seul → affiche le statut
    if option is None:
        if user_id in notified_users:
            await ctx.send(f"🔔 {ctx.author.mention}, tu es **actuellement inscrit** aux rappels.")
        else:
            await ctx.send(f"🔕 {ctx.author.mention}, tu n’es **pas inscrit** aux rappels.")
        return

    # ✅ Cas 2 : !notify on → inscription
    if option.lower() == "on":
        if user_id in notified_users:
            await ctx.send(f"✅ {ctx.author.mention}, tu es **déjà inscrit** aux rappels.")
        else:
            notified_users.append(user_id)
            with open("notified_users.json", "w") as f:
                json.dump(notified_users, f)
            await ctx.send(f"🔔 {ctx.author.mention}, tu es maintenant **inscrit** aux rappels.")
        return

    # ✅ Cas 3 : !notify off → désinscription
    if option.lower() == "off":
        if user_id in notified_users:
            notified_users.remove(user_id)
            with open("notified_users.json", "w") as f:
                json.dump(notified_users, f)
            await ctx.send(f"❌ {ctx.author.mention}, tu es maintenant **désinscrit** des rappels.")
        else:
            await ctx.send(f"ℹ️ {ctx.author.mention}, tu n’étais pas inscrit.")
        return

    # ✅ Cas 4 : Mauvaise syntaxe
    await ctx.send("⚠️ Utilisation : `!notify`, `!notify on` ou `!notify off`")

# ============ 🕒 RAPPPELS AUTOMATIQUES DES ÉVÉNEMENTS DISCORD ============
@tasks.loop(minutes=1)
async def check_meetings():
    """Vérifie les événements Discord planifiés et envoie un rappel 5 min avant aux intéressés non connectés"""
    now = datetime.now(timezone.utc)
    for guild in bot.guilds:
        events = await guild.fetch_scheduled_events()
        for event in events:
            if event.status != discord.EventStatus.scheduled:
                continue
            start_time = get_event_start_time(event)
            if start_time is None:
                continue
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

# ============ 🧵 FERMETURE ET ARCHIVAGE AUTOMATIQUE DES POSTS ============

@bot.event
async def on_thread_update(before: discord.Thread, after: discord.Thread):
    """Détecte quand un post est fermé puis l’archive 24h plus tard."""
    try:
        # Vérifie que le thread vient d’être fermé
        if before.locked is False and after.locked is True:
            print(f"🧵 Le post '{after.name}' a été fermé. Archivage prévu dans 24h.")
            await after.send("🔒 Ce post a été **fermé**. Il sera archivé automatiquement dans 24 heures.")
            await asyncio.sleep(86400)  # 24 heures
            # Vérifie que le post n’a pas été rouvert entre temps
            refreshed = await after.guild.fetch_channel(after.id)
            if not refreshed.archived:
                await refreshed.edit(archived=True)
                await refreshed.send("📦 Ce post a été **archivé automatiquement** après 24 heures.")
                print(f"✅ Post '{after.name}' archivé automatiquement après 24h.")
    except Exception as e:
        print(f"⚠️ Erreur lors de l’archivage automatique du post : {e}")


@bot.command(name="close")
async def close_thread(ctx):
    """Ferme le post/forum actuel et programme son archivage automatique dans 24h."""
    thread = ctx.channel

    if not isinstance(thread, discord.Thread):
        await ctx.send("⚠️ Cette commande ne peut être utilisée **que dans un post de forum**.")
        return

    # Vérifie si le thread est déjà fermé
    if thread.locked:
        await ctx.send("🔒 Ce post est **déjà fermé**.")
        return

    try:
        await thread.edit(locked=True)
        await ctx.send("✅ Ce post a été **fermé**. Il sera archivé automatiquement dans 24 heures.")
        print(f"🧵 Post '{thread.name}' fermé manuellement par {ctx.author}. Archivage dans 24h.")
        await asyncio.sleep(86400)
        refreshed = await thread.guild.fetch_channel(thread.id)
        if not refreshed.archived:
            await refreshed.edit(archived=True)
            await refreshed.send("📦 Ce post a été **archivé automatiquement** après 24 heures.")
            print(f"✅ Post '{thread.name}' archivé automatiquement après 24h.")
    except Exception as e:
        await ctx.send("⚠️ Impossible de fermer ou archiver ce post.")
        print(f"Erreur lors de la fermeture manuelle du post : {e}")


if __name__ == "__main__":
    bot.run(TOKEN)
