
# =========== 📦 IMPORTS ============

import re
import os
import requests
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import json
import pytz
import asyncio
from types import SimpleNamespace
import logging

# Configure basic logging so we reliably see runtime messages
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('EpiTrelloBot')

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

# Mapping guild_id -> channel_id for forced reminder channel per guild
reminder_channels = {}

closing_cache = {}
closed_threads = {}
bot_closed_threads = set()  # IDs of threads closed by the bot command (temporary)

# =========== 💾 GESTION FICHIERS ============

# Load reminder channel overrides from disk
def load_reminder_channels():
    global reminder_channels
    path = os.path.join(os.getcwd(), 'reminder_channels.json')
    if not os.path.exists(path):
        reminder_channels = {}
        return
    try:
        with open(path, 'r') as f:
            reminder_channels = json.load(f)
    except Exception:
        reminder_channels = {}


def save_reminder_channels():
    path = os.path.join(os.getcwd(), 'reminder_channels.json')
    try:
        with open(path, 'w') as f:
            json.dump(reminder_channels, f)
    except Exception as e:
        logger.error(f"Impossible d'enregistrer reminder_channels.json: {e}")


# Charger notified_users.json en mémoire au démarrage
def load_notified_users():
    global notify_opt_out
    path = os.path.join(os.getcwd(), 'notified_users.json')
    if not os.path.exists(path):
        notify_opt_out = set()
        return
    try:
        with open(path, 'r') as f:
            data = json.load(f)
            # Normalize IDs to int where possible
            normalized = set()
            for v in data:
                try:
                    normalized.add(int(v))
                except Exception:
                    # keep original if cannot convert
                    try:
                        normalized.add(v)
                    except Exception:
                        pass
            notify_opt_out = normalized
    except Exception:
        notify_opt_out = set()


def save_notified_users():
    path = os.path.join(os.getcwd(), 'notified_users.json')
    try:
        with open(path, 'w') as f:
            json.dump(list(notify_opt_out), f)
    except Exception as e:
        print(f"⚠️ Impossible d'enregistrer notified_users.json: {e}")


# Load/save for closed threads (stores closure timestamp in ISO format)
def load_closed_threads():
    global closed_threads, closing_cache
    path = os.path.join(os.getcwd(), 'closed_threads.json')
    if not os.path.exists(path):
        closed_threads = {}
        return
    try:
        with open(path, 'r') as f:
            data = json.load(f)
            if isinstance(data, dict):
                closed_threads = data
            else:
                closed_threads = {}
    except Exception:
        closed_threads = {}

    # seed closing_cache with parsed datetimes where possible
    for k, v in list(closed_threads.items()):
        try:
            tid = int(k)
            dt = None
            if isinstance(v, str):
                try:
                    dt = datetime.fromisoformat(v)
                except Exception:
                    dt = None
            closing_cache[tid] = dt
        except Exception:
            # skip entries that can't be parsed
            pass

def save_closed_threads():
    path = os.path.join(os.getcwd(), 'closed_threads.json')
    try:
        with open(path, 'w') as f:
            json.dump(closed_threads, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Impossible d'enregistrer closed_threads.json: {e}")


async def send_confirmation_outside_thread(ctx, thread, content):
    """Try to send a confirmation message outside the thread to avoid unarchiving it.

    Order: DM the command author, then guild.system_channel (if available and sendable),
    then send in the invoking channel only if it's not the thread.
    Returns True if a message was sent, False otherwise.
    """
    # 1) DM the author
    try:
        await ctx.author.send(content)
        return True
    except Exception:
        pass

    # 2) system channel
    try:
        sc = ctx.guild.system_channel
        if sc and getattr(sc, 'send', None):
            perms = sc.permissions_for(ctx.guild.me)
            if perms and perms.send_messages and sc != thread:
                await sc.send(content)
                return True
    except Exception:
        pass

    # 3) if the invoking channel is different from the thread, send there
    try:
        if ctx.channel and getattr(ctx.channel, 'id', None) != getattr(thread, 'id', None):
            await ctx.send(content)
            return True
    except Exception:
        pass

    return False


# Charger les opt-out en mémoire maintenant
load_notified_users()
load_reminder_channels()
load_closed_threads()

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


async def get_event_interested_users(guild: discord.Guild, event) -> list:
    """Return a list of User objects who are interested in the event.

    This handles multiple discord.py versions:
      - event.fetch_users() (newer)
      - guild.fetch_scheduled_event_users(event.id) (alternate)
    The function normalizes different return shapes.
    """
    # Try event.fetch_users()
    fetch_attr = getattr(event, 'fetch_users', None)
    if callable(fetch_attr):
        try:
            users = [u async for u in event.fetch_users()]
            return users
        except Exception:
            # Fall through to other methods
            pass

    # Try guild.fetch_scheduled_event_users
    guild_fetch = getattr(guild, 'fetch_scheduled_event_users', None)
    if callable(guild_fetch):
        try:
            res = await guild.fetch_scheduled_event_users(event.id)
            # res might be (users, next_token) or a list
            users = res
            if isinstance(res, tuple) and len(res) > 0:
                users = res[0]

            normalized = []
            for item in users:
                # item might be a ScheduledEventUser with .user
                if hasattr(item, 'user'):
                    normalized.append(item.user)
                else:
                    normalized.append(item)
            return normalized
        except Exception:
            pass

    # Last resort: try attribute 'users' or 'user' lists on event
    if hasattr(event, 'users') and isinstance(event.users, list):
        return event.users

    # Final fallback: call Discord REST API directly if we have a bot token
    if TOKEN:
        try:
            url = f"https://discord.com/api/v10/guilds/{guild.id}/scheduled-events/{event.id}/users?with_member=true&limit=100"
            headers = {
                "Authorization": f"Bot {TOKEN}",
                "Accept": "application/json",
                "User-Agent": "EpiTrelloBot (https://github.com/ErwannL/EpiTrelloBot, 1.0)"
            }
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                normalized = []
                for item in data:
                    # item could be {user: {...}, member: {...}} or a user object
                    user_obj = None
                    if isinstance(item, dict) and 'user' in item and isinstance(item['user'], dict):
                        u = item['user']
                        uid = int(u.get('id'))
                        username = u.get('username') or u.get('name') or str(uid)
                        display = username
                        # member nickname if present
                        if 'member' in item and isinstance(item['member'], dict):
                            display = item['member'].get('nick') or username
                        user_obj = SimpleNamespace(id=uid, name=username, display_name=display, mention=f"<@{uid}>")
                    elif isinstance(item, dict) and 'id' in item:
                        uid = int(item.get('id'))
                        username = item.get('username') or item.get('name') or str(uid)
                        user_obj = SimpleNamespace(id=uid, name=username, display_name=username, mention=f"<@{uid}>")
                    if user_obj:
                        normalized.append(user_obj)
                return normalized
        except Exception:
            pass

    return []


def _get_channel_by_id(guild: discord.Guild, cid: int):
    # Try guild cache first, then bot cache
    ch = guild.get_channel(cid)
    if ch:
        return ch
    return bot.get_channel(cid)


def get_reminder_channel(guild: discord.Guild, event):
    """Return a channel object where reminders should be sent for this guild/event.
    Priority:
      - per-guild override in reminder_channels.json
      - event.channel if it's sendable
      - guild.system_channel
      - first text channel where the bot has send_messages permission
    """
    # per-guild override
    gid = str(guild.id)
    if gid in reminder_channels:
        try:
            cid = int(reminder_channels[gid])
            ch = _get_channel_by_id(guild, cid)
            if ch and hasattr(ch, 'send'):
                perms = ch.permissions_for(guild.me)
                if perms and perms.send_messages:
                    return ch
        except Exception:
            pass

    # prefer event channel when it's sendable
    ch = event.channel if getattr(event, 'channel', None) is not None else None
    if ch and hasattr(ch, 'send'):
        perms = ch.permissions_for(guild.me) if hasattr(ch, 'permissions_for') else None
        if not perms or (perms and getattr(perms, 'send_messages', True)):
            return ch

    # fallback system channel
    if guild.system_channel and hasattr(guild.system_channel, 'send'):
        perms = guild.system_channel.permissions_for(guild.me)
        if perms and perms.send_messages:
            return guild.system_channel

    # last fallback: first text channel bot can send to
    for c in getattr(guild, 'text_channels', []):
        perms = c.permissions_for(guild.me)
        if perms and perms.send_messages:
            return c

    return None


def _chunks_from_lines(msg: str, max_len: int = 1800):
    """Split `msg` into chunks no longer than `max_len`, but only at line boundaries.

    This prevents cutting a logical line (or sentence) in the middle when sending
    long Discord messages.
    """
    if not msg:
        return []
    lines = msg.splitlines(keepends=True)
    chunks = []
    cur = []
    cur_len = 0
    for line in lines:
        # If adding this line would overflow, flush current chunk first
        if cur_len + len(line) > max_len:
            if cur:
                chunks.append(''.join(cur))
                cur = []
                cur_len = 0
            # If single line is longer than max_len, put it alone in a chunk
            if len(line) > max_len:
                chunks.append(line)
                continue
        cur.append(line)
        cur_len += len(line)

    if cur:
        chunks.append(''.join(cur))
    return chunks

async def fetch_all_threads(channel: discord.ForumChannel):
    """Récupère tous les threads d'un ForumChannel.

    Essaie d'utiliser l'API client (channel.fetch_threads) si disponible.
    Sinon, utilise l'API REST via requests et le token BOT (global TOKEN) pour récupérer
    active + archived (public/private) threads. Retourne une liste d'objets avec
    attributs utilisés ailleurs (id, name, archived, locked, created_at, message_count, parent).
    """
    threads = []

    # 1) If the library provides channel.fetch_threads, use it (async)
    fetch_attr = getattr(channel, 'fetch_threads', None)
    if callable(fetch_attr):
        try:
            fetched = await channel.fetch_threads(limit=100)
            threads.extend(fetched.threads)
            while getattr(fetched, 'has_more', False):
                fetched = await channel.fetch_threads(after=fetched.threads[-1].id)
                threads.extend(fetched.threads)
            return threads
        except Exception:
            # fall through to HTTP fallback
            pass

    # 2) HTTP fallback using the REST endpoints (requires TOKEN)
    if not TOKEN:
        return threads

    def _snowflake_time(sid: int):
        try:
            sid = int(sid)
            ts = ((sid >> 22) + 1420070400000) / 1000
            return datetime.fromtimestamp(ts, timezone.utc)
        except Exception:
            return None

    headers = {
        "Authorization": f"Bot {TOKEN}",
        "Accept": "application/json",
        "User-Agent": "EpiTrelloBot (fetch_threads fallback)"
    }

    base = "https://discord.com/api/v10"

    # Helper to convert REST thread dict -> SimpleNamespace-like object
    def _mk_thread_obj(tdata):
        tid = int(tdata.get('id'))
        # Normalize archived/locked which can be bool or strings in some responses
        def _to_bool(val):
            if isinstance(val, bool):
                return val
            if val is None:
                return False
            s = str(val).lower()
            return s in ('1', 'true', 'yes')

        # Prefer thread_metadata if present (REST thread objects nest archived/locked there)
        meta = tdata.get('thread_metadata') or tdata.get('metadata') or {}
        archived_val = meta.get('archived', tdata.get('archived', False))
        locked_val = meta.get('locked', tdata.get('locked', False))
        archived = _to_bool(archived_val)
        locked = _to_bool(locked_val)

        # message_count may be absent
        msg_count = tdata.get('message_count') if 'message_count' in tdata else '?'

        return SimpleNamespace(
            id=tid,
            name=tdata.get('name') or f"<{tid}>",
            archived=archived,
            locked=locked,
            created_at=_snowflake_time(tdata.get('id')),
            message_count=msg_count,
            parent=channel
        )

    try:
        # Active threads
        url_active = f"{base}/channels/{channel.id}/threads/active"
        r = requests.get(url_active, headers=headers, timeout=10)
        if r.status_code == 200:
            j = r.json()
            for td in j.get('threads', []):
                threads.append(_mk_thread_obj(td))

        # Archived public threads (paginated)
        url_archived_public = f"{base}/channels/{channel.id}/threads/archived/public"
        params = {'limit': 100}
        while True:
            r = requests.get(url_archived_public, headers=headers, params=params, timeout=10)
            if r.status_code != 200:
                break
            j = r.json()
            for td in j.get('threads', []):
                threads.append(_mk_thread_obj(td))
            if not j.get('has_more'):
                break
            # use 'before' param with last thread id to paginate
            last = j.get('threads', [])[-1].get('id') if j.get('threads') else None
            if not last:
                break
            params['before'] = last

        # Archived private threads (if bot has access)
        url_archived_private = f"{base}/channels/{channel.id}/threads/archived/private"
        params = {'limit': 100}
        while True:
            r = requests.get(url_archived_private, headers=headers, params=params, timeout=10)
            if r.status_code != 200:
                break
            j = r.json()
            for td in j.get('threads', []):
                threads.append(_mk_thread_obj(td))
            if not j.get('has_more'):
                break
            last = j.get('threads', [])[-1].get('id') if j.get('threads') else None
            if not last:
                break
            params['before'] = last

    except Exception:
        return threads

    # Deduplicate by id
    # Prefer non-archived thread objects when a thread appears both in active and archived results
    unique = {}
    for t in threads:
        existing = unique.get(t.id)
        if existing is None:
            unique[t.id] = t
            continue

        # If existing is archived but new one is not, prefer the new one
        existing_arch = getattr(existing, 'archived', False)
        new_arch = getattr(t, 'archived', False)
        if existing_arch and not new_arch:
            unique[t.id] = t
            continue

        # If both have same archived state, prefer the one with a created_at value
        if existing_arch == new_arch:
            if getattr(existing, 'created_at', None) is None and getattr(t, 'created_at', None) is not None:
                unique[t.id] = t
            # otherwise keep existing (it may already be the active representation)

    return list(unique.values())


async def get_lock_date(thread_id: int, guild: discord.Guild):
    # Vérifier cache
    if thread_id in closing_cache:
        return closing_cache[thread_id]

    lock_date = None
    try:
        async for entry in guild.audit_logs(
            limit=100,  # tu peux augmenter
            action=discord.AuditLogAction.thread_update
        ):
            if entry.target.id != thread_id:
                continue

            before_locked = getattr(entry.before, "locked", None)
            after_locked = getattr(entry.after, "locked", None)
            if before_locked is False and after_locked is True:
                lock_date = entry.created_at
                break
    except discord.Forbidden:
        print(f"[DEBUG-LOCK] Pas de permission pour lire audit_logs")
    except Exception as e:
        print(f"[DEBUG-LOCK] Erreur inattendue: {e}")

    closing_cache[thread_id] = lock_date
    return lock_date


# ---- Background task: purge closed threads older than 1 week ----
@tasks.loop(minutes=60)
async def purge_closed_threads():
    """Delete threads listed in `closed_threads` if they were closed more than 1 week ago.

    Runs periodically and removes successful/irrecoverable entries from `closed_threads.json`.
    """
    now = datetime.now(timezone.utc)
    to_delete = []

    # Collect candidate thread IDs
    for tid_str, iso in list(closed_threads.items()):
        try:
            tid = int(tid_str)
        except Exception:
            continue

        dt = None
        # Prefer cached parsed datetime
        if tid in closing_cache and closing_cache[tid] is not None:
            dt = closing_cache[tid]
        else:
            if isinstance(iso, str):
                try:
                    dt = datetime.fromisoformat(iso)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except Exception:
                    dt = None

        if not dt:
            # can't determine close date; skip for now
            continue

        if now >= dt + timedelta(weeks=1):
            to_delete.append((tid, dt))

    # Attempt deletion
    for tid, dt in to_delete:
        ch = bot.get_channel(tid)
        if ch is None:
            # try fetch via bot (may fail if not available)
            try:
                ch = await bot.fetch_channel(tid)
            except Exception:
                ch = None

        # fallback: try per-guild fetch
        if ch is None:
            for g in bot.guilds:
                try:
                    ch = await g.fetch_channel(tid)
                    if ch:
                        break
                except Exception:
                    ch = None

        if ch is None:
            # channel not found anymore: remove from closed_threads
            closed_threads.pop(str(tid), None)
            closing_cache.pop(tid, None)
            try:
                save_closed_threads()
            except Exception:
                logger.warning(f"Auto-purge: couldn't save closed_threads after removing {tid}")
            logger.info(f"Auto-purge: thread {tid} not found; removed from closed_threads.json")
            continue

        try:
            await ch.delete(reason="Auto-deleted: closed > 1 week")
        except Exception as e:
            logger.warning(f"Auto-purge: failed to delete thread {tid}: {e}")
            # don't remove the entry so we can retry later
            continue

        # deletion succeeded: remove record and persist
        closed_threads.pop(str(tid), None)
        closing_cache.pop(tid, None)
        try:
            save_closed_threads()
        except Exception:
            logger.warning(f"Auto-purge: couldn't save closed_threads after deleting {tid}")

        logger.info(f"Auto-purge: deleted thread {tid} (closed at {dt.isoformat()})")

        # Optional: notify guild.system_channel if available and sendable
        try:
            guild = getattr(ch, 'guild', None)
            if guild and getattr(guild, 'system_channel', None):
                sc = guild.system_channel
                perms = sc.permissions_for(guild.me)
                if perms and perms.send_messages:
                    await sc.send(f"🗑️ Le post '{getattr(ch,'name', str(tid))}' a été supprimé automatiquement (fermé depuis plus d'une semaine).")
        except Exception:
            pass


# ============ 🚀 ÉVÉNEMENTS ============

@bot.event
async def on_ready():
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[RELOAD] {now} — ✅ Connecté en tant que {bot.user}")
    # Start periodic background tasks
    try:
        if not purge_closed_threads.is_running():
            purge_closed_threads.start()
    except Exception:
        pass
    # check_meetings.start()
    # await check_old_closed_threads()


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
    # Charger l'état en mémoire (notify_opt_out)
    global notify_opt_out

    # Cas 1 : !notify seul → affiche le statut
    # NOTE: notify_opt_out now stores users who DO NOT want notifications.
    if option is None:
        if user_id in notify_opt_out:
            await ctx.send(f"� {ctx.author.mention}, tu es **désinscrit** des rappels (opt-out).")
        else:
            await ctx.send(f"� {ctx.author.mention}, tu es **inscrit** aux rappels par défaut.")
        return

    # Cas 2 : !notify on → (re)inscription aux rappels — enlever du opt-out
    if option.lower() == "on":
        if user_id not in notify_opt_out:
            await ctx.send(f"✅ {ctx.author.mention}, tu es déjà **inscrit** aux rappels.")
        else:
            try:
                notify_opt_out.remove(user_id)
            except KeyError:
                pass
            save_notified_users()
            await ctx.send(f"🔔 {ctx.author.mention}, tu es maintenant **inscrit** aux rappels.")
        return

    # Cas 3 : !notify off → désinscription (ajout au opt-out)
    if option.lower() == "off":
        if user_id in notify_opt_out:
            await ctx.send(f"ℹ️ {ctx.author.mention}, tu es déjà **désinscrit** des rappels.")
        else:
            notify_opt_out.add(user_id)
            save_notified_users()
            await ctx.send(f"❌ {ctx.author.mention}, tu es maintenant **désinscrit** des rappels.")
        return

    # Cas 4 : Mauvaise syntaxe
    await ctx.send("⚠️ Utilisation : `!notify`, `!notify on` ou `!notify off`")


# ============ 🔐 COMMANDES ADMIN (TEST) ============

@bot.group(name="admin", invoke_without_command=True)
@commands.has_permissions(administrator=True)
async def admin(ctx):
    """Groupe de commandes admin pour tester le bot."""
    await ctx.send("Utilisation: `!admin health | github [pr_number] | notified | guilds` (admin seulement)")


@admin.command(name="health")
@commands.has_permissions(administrator=True)
async def admin_health(ctx):
    """Vérifie rapidement l'état des variables d'environnement et dépendances."""
    checks = {}
    # Env vars
    checks['GITHUB_REPO'] = bool(GITHUB_REPO)
    checks['GITHUB_TOKEN'] = bool(GITHUB_TOKEN)
    checks['GITHUB_PROJECT'] = bool(GITHUB_PROJECT)

    # Packages availability (runtime)
    pkgs = {}
    for pkg in ('requests','discord','pytz'):
        try:
            __import__(pkg)
            pkgs[pkg] = 'ok'
        except Exception as e:
            pkgs[pkg] = f'missing ({e.__class__.__name__})'

    latency = round(bot.latency * 1000) if bot.latency is not None else 'N/A'

    lines = ["**Health check rapide**"]
    for k,v in checks.items():
        lines.append(f"• {k}: {'set' if v else 'NOT SET'}")
    lines.append("\n**Packages:**")
    for k,v in pkgs.items():
        lines.append(f"• {k}: {v}")
    lines.append(f"\n• Latence websocket: {latency} ms")

    await ctx.send("\n".join(lines))


@admin.command(name="github")
@commands.has_permissions(administrator=True)
async def admin_github(ctx, pr_number: int = None):
    """Test l'accès GitHub: sans argument vérifie le repo, avec un numéro récupère la PR."""
    if not GITHUB_REPO:
        await ctx.send("⚠️ `GITHUB_REPO` non configuré.")
        return

    if pr_number is None:
        url = f"https://api.github.com/repos/{GITHUB_REPO}"
        try:
            r = requests.get(url, headers=github_headers(), timeout=10)
        except requests.RequestException as e:
            await ctx.send(f"⚠️ Erreur requête GitHub: {e}")
            return

        if r.status_code == 200:
            data = r.json()
            await ctx.send(f"✅ Accès repo OK — {data.get('full_name')} — {data.get('private') and 'private' or 'public'}")
        else:
            await ctx.send(f"❌ Erreur {r.status_code} lors de l'accès au repo.")
    else:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/pulls/{pr_number}"
        try:
            r = requests.get(url, headers=github_headers(), timeout=10)
        except requests.RequestException as e:
            await ctx.send(f"⚠️ Erreur requête GitHub: {e}")
            return

        if r.status_code == 200:
            data = r.json()
            await ctx.send(f"✅ PR #{pr_number} trouvée: {data.get('title','(no title)')} — {data.get('html_url')}")
        elif r.status_code == 404:
            await ctx.send(f"❌ PR #{pr_number} introuvable.")
        else:
            await ctx.send(f"⚠️ Erreur GitHub {r.status_code}.")


@admin.command(name="notified")
@commands.has_permissions(administrator=True)
async def admin_notified(ctx):
    """Affiche le nombre et un échantillon d'utilisateurs notifiés (fichier notified_users.json)."""
    path = os.path.join(os.getcwd(), 'notified_users.json')
    if not os.path.exists(path):
        return await ctx.send("ℹ️ Aucun fichier `notified_users.json` trouvé.")

    try:
        with open(path, 'r') as f:
            users = json.load(f)
    except Exception as e:
        return await ctx.send(f"⚠️ Impossible de lire le fichier: {e}")

    # Now this file stores the opt-out users (those who DO NOT want notifications)
    sample = users[:10]
    await ctx.send(f"👥 {len(users)} utilisateurs désinscrits (opt-out) (exemple: {sample})")


@admin.command(name="guilds")
@commands.has_permissions(administrator=True)
async def admin_guilds(ctx):
    """Liste les guildes où le bot est présent (id + nom)."""
    lines = [f"Guildes ({len(bot.guilds)}):"]
    for g in bot.guilds:
        lines.append(f"• {g.name} — {g.id}")
    await ctx.send("\n".join(lines))


@admin.error
async def admin_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Tu dois être administrateur pour utiliser ces commandes.")
    else:
        await ctx.send(f"⚠️ Erreur: {error}")


# ============ 🔎 COMMANDES ADMIN: VOICE / EVENT / SIMULATE / DEBUG THREADS ============

@admin.command(name="voice")
@commands.has_permissions(administrator=True)
async def admin_voice(ctx, *, channel: str = None):
    """Liste les membres d'un salon vocal.

    Usage: !admin voice <channel_id|channel_name|mention>
    Si aucun argument, liste les membres du canal vocal actuel (si applicable).
    """
    # Trouver le channel
    target = None
    if channel is None:
        # si l'auteur est dans un vocal, prendre celui-ci
        if hasattr(ctx.author, 'voice') and ctx.author.voice and ctx.author.voice.channel:
            target = ctx.author.voice.channel
    else:
        # essayer ID
        ch = None
        if channel.isdigit():
            ch = ctx.guild.get_channel(int(channel))
        if ch is None:
            # par mention/name
            # on accepte un mention comme <#id>
            m = re.match(r"<#(\d+)>", channel)
            if m:
                ch = ctx.guild.get_channel(int(m.group(1)))
        if ch is None:
            # trouver par nom
            for c in ctx.guild.voice_channels:
                if c.name.lower() == channel.lower():
                    ch = c
                    break
        target = ch

    if target is None:
        return await ctx.send("⚠️ Salon vocal introuvable. Mentionne ou donne l'ID/nom, ou rejoins un vocal et lance la commande sans argument.")

    members = target.members
    if not members:
        return await ctx.send(f"🔈 Salon '{target.name}' vide.")

    lines = [f"🔈 Membres dans '{target.name}' ({len(members)}):"]
    for m in members:
        lines.append(f"• {m} — {m.id}")
    await ctx.send("\n".join(lines))


@admin.command(name="event")
@commands.has_permissions(administrator=True)
async def admin_event(ctx, event_id: int = None):
    """Liste les utilisateurs intéressés par un événement planifié.

    Usage: !admin event <event_id>
    Si pas d'ID, liste les events du guild et leurs IDs.
    """
    guild = ctx.guild
    if event_id is None:
        events = await guild.fetch_scheduled_events()
        if not events:
            return await ctx.send("Aucun événement prévu sur cette guild.")
        lines = ["📅 Événements planifiés :"]
        for e in events:
            start = get_event_start_time(e)
            start_str = start.strftime('%d/%m %H:%M') if start else '??'
            lines.append(f"• {e.name} — id:{e.id} — {start_str}")
        return await ctx.send("\n".join(lines))

    # Récupérer l'event
    try:
        event = await guild.fetch_scheduled_event(event_id)
    except Exception as e:
        return await ctx.send(f"⚠️ Impossible de récupérer l'événement: {e}")
    # Diagnostic: try multiple retrievals and report results to help debug empty lists
    diag = []

    has_event_fetch = callable(getattr(event, 'fetch_users', None))
    has_guild_fetch = callable(getattr(guild, 'fetch_scheduled_event_users', None))
    diag.append(f"has event.fetch_users: {has_event_fetch}")
    diag.append(f"has guild.fetch_scheduled_event_users: {has_guild_fetch}")

    # Try event.fetch_users() if available
    event_fetch_count = None
    event_fetch_error = None
    if has_event_fetch:
        try:
            tmp = [u async for u in event.fetch_users()]
            event_fetch_count = len(tmp)
        except Exception as e:
            event_fetch_error = str(e)

    # Try guild.fetch_scheduled_event_users if available
    guild_fetch_count = None
    guild_fetch_result_info = None
    if has_guild_fetch:
        try:
            res = await guild.fetch_scheduled_event_users(event.id)
            # res may be (users, next_token) or a list-like
            users = res
            if isinstance(res, tuple) and len(res) > 0:
                users = res[0]
            guild_fetch_count = len(users) if hasattr(users, '__len__') else None
            guild_fetch_result_info = type(res).__name__
        except Exception as e:
            guild_fetch_result_info = f"error: {e}"

    # Check fallback attribute on event
    event_users_attr_len = None
    if hasattr(event, 'users') and isinstance(event.users, (list, tuple)):
        try:
            event_users_attr_len = len(event.users)
        except Exception:
            event_users_attr_len = None

    try:
        interested = await get_event_interested_users(guild, event)
    except Exception as e:
        return await ctx.send(f"⚠️ Erreur lors de la récupération des utilisateurs intéressés: {e}")

    lines = [f"📅 Intéressés pour '{event.name}' ({len(interested)}):"]
    sample = interested[:50]
    for u in sample:
        lines.append(f"• {getattr(u,'display_name', getattr(u,'name', str(u)))} — {u.id}")
    if len(interested) > len(sample):
        lines.append(f"... et {len(interested)-len(sample)} de plus")

    await ctx.send("\n".join(lines))


@admin.command(name="simulate")
@commands.has_permissions(administrator=True)
async def admin_simulate(ctx, event_id: int = None):
    """Simule la logique de check_meetings pour un event donné — liste qui serait pingué.

    Si aucun `event_id` fourni, liste les events disponibles pour l'aider.
    """
    guild = ctx.guild
    if event_id is None:
        events = await guild.fetch_scheduled_events()
        if not events:
            return await ctx.send("Aucun événement prévu sur cette guild.")
        lines = ["📅 Événements planifiés :"]
        for e in events:
            start = get_event_start_time(e)
            start_str = start.strftime('%d/%m %H:%M') if start else '??'
            lines.append(f"• {e.name} — id:{e.id} — {start_str}")
        return await ctx.send("\n".join(lines))

    try:
        event = await guild.fetch_scheduled_event(event_id)
    except Exception as e:
        return await ctx.send(f"⚠️ Impossible de récupérer l'événement: {e}")

    interested = await get_event_interested_users(guild, event)

    # who is already connected in event.channel if voice
    already_connected = []
    if isinstance(event.channel, discord.VoiceChannel):
        already_connected = [m for m in event.channel.members]

    users_to_ping = [u for u in interested if getattr(u,'id', None) not in notify_opt_out and all(getattr(u,'id', None) != m.id for m in already_connected)]

    lines = [f"🔔 Simulation pour '{event.name}':"]
    lines.append(f"• Intéressés: {len(interested)}")
    lines.append(f"• Déjà connectés: {len(already_connected)}")
    lines.append(f"• Opt-out: {len([u for u in interested if getattr(u,'id', None) in notify_opt_out])}")
    lines.append(f"• À pinguer: {len(users_to_ping)}")
    if users_to_ping:
        lines.append("Exemple (max 20):")
        for u in users_to_ping[:20]:
            lines.append(f"• {getattr(u,'display_name', getattr(u,'name', str(u)))} — {u.id}")

    await ctx.send("\n".join(lines))


@admin.command(name="setreminder")
@commands.has_permissions(administrator=True)
async def admin_setreminder(ctx, channel_id: int):
    """Set the reminder channel for this guild. Usage: !admin setreminder <channel_id>"""
    gid = str(ctx.guild.id)
    # validate channel
    ch = _get_channel_by_id(ctx.guild, channel_id)
    if not ch or not hasattr(ch, 'send'):
        return await ctx.send("⚠️ Salon introuvable ou non-textuel dans cette guild.")
    perms = ch.permissions_for(ctx.guild.me)
    if not (perms and perms.send_messages):
        return await ctx.send("⚠️ Je n'ai pas la permission d'envoyer des messages dans ce salon.")

    reminder_channels[gid] = channel_id
    save_reminder_channels()
    await ctx.send(f"✅ Canal de rappel configuré pour cette guild: {getattr(ch,'name', channel_id)} ({channel_id})")


@admin.command(name="clearreminder")
@commands.has_permissions(administrator=True)
async def admin_clearreminder(ctx):
    """Clear the reminder channel override for this guild."""
    gid = str(ctx.guild.id)
    if gid in reminder_channels:
        reminder_channels.pop(gid, None)
        save_reminder_channels()
        await ctx.send("✅ Override de canal de rappel supprimé pour cette guild. La sélection par défaut sera utilisée.")
    else:
        await ctx.send("ℹ️ Aucun override défini pour cette guild.")


@admin.command(name="remind")
@commands.has_permissions(administrator=True)
async def admin_remind(ctx, event_id: int):
    """Force l'envoi immédiat d'un rappel pour un event (admin only). Usage: !admin remind <event_id>"""
    
    guild = ctx.guild

    # ---- Récupération de l'événement ----
    try:
        event = await guild.fetch_scheduled_event(event_id)
    except Exception as e:
        return await ctx.send(f"⚠️ Impossible de récupérer l'événement : {e}")

    # ---- Sélection du channel ----
    channel = get_reminder_channel(guild, event)
    if not channel or not hasattr(channel, "send"):
        return await ctx.send(
            f"⚠️ Aucun channel textuel disponible pour envoyer le rappel de **{event.name}**."
        )

    # ---- Récupération des participants ----
    interested_users = await get_event_interested_users(guild, event)

    already_connected = []
    if isinstance(event.channel, discord.VoiceChannel):
        already_connected = [m for m in event.channel.members]

    # ---- Filtrer les utilisateurs : pas opt-out + pas déjà en vocal ----
    users_to_ping = [
        u.mention
        for u in interested_users
        if getattr(u, "id", None) not in notify_opt_out
        and all(getattr(u, "id", None) != m.id for m in already_connected)
    ]

    if not users_to_ping:
        return await ctx.send(
            f"ℹ️ Aucun utilisateur à ping pour **{event.name}** "
            "(tous déjà connectés ou opt-out)."
        )

    # ---- Génération des mentions ----
    mentions = " ".join(users_to_ping)

    # ---- Embed ----
    embed = discord.Embed(
        title=f"⏰ Rappel : {event.name}",
        description=f"Rappel forcé par admin.\nParticipants notifiés : {mentions}",
        color=0x5865F2,
        timestamp=get_event_start_time(event) or datetime.now(timezone.utc),
    )

    target_channel_name = getattr(channel, "name", None) or str(getattr(channel, "id", "N/A"))
    embed.set_footer(text=f"Envoyé par {ctx.author} | channel : {target_channel_name}")

    # ---- Envoi du message ----
    try:
        # Autoriser les pings d'utilisateurs
        allowed_ping = discord.AllowedMentions(users=True)

        # 2️⃣ Envoi de l’embed (sans aucun ping)
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        # 1️⃣ Envoi du message texte avec les VRAIS pings
        await channel.send(content=mentions, allowed_mentions=allowed_ping)

        return await ctx.send(
            f"✅ Rappel forcé envoyé dans **{target_channel_name}** "
            f"(ping de **{len(users_to_ping)}** utilisateurs)."
        )

    except Exception as e:
        logger.error(f"Erreur envoi rappel forcé pour {event.name}: {e}")
        return await ctx.send(f"⚠️ Erreur en envoyant le rappel forcé : {e}")

# ============ 🕒 RAPPPELS AUTOMATIQUES DES ÉVÉNEMENTS DISCORD ============


# ============ 🧵 FERMETURE ET ARCHIVAGE AUTOMATIQUE DES POSTS ============

@admin.command(name="debugthreads")
@commands.has_permissions(administrator=True)
async def admin_debugthreads(ctx):
    """Debug les threads fermés non archivés. Usage: !admin debugthreads"""
    guild = ctx.guild
    all_threads = []
    # Collect threads from every ForumChannel (fetch to include closed/archived)
    for channel in guild.channels:
        if isinstance(channel, discord.ForumChannel):
            try:
                fetched = await fetch_all_threads(channel)
            except Exception as e:
                await ctx.send(f"⚠️ Erreur forum {getattr(channel,'name',channel.id)}: {e}")
                continue
            for t in fetched:
                # ensure parent is set for context
                if not getattr(t, 'parent', None):
                    t.parent = channel
                all_threads.append(t)

    if not all_threads:
        return await ctx.send("Aucun post trouvé sur ce serveur.")

    # Deduplicate by id
    uniq = {t.id: t for t in all_threads}
    threads_list = list(uniq.values())

    # Sort by creation date if available (newest last)
    def _key_created(th):
        dt = getattr(th, 'created_at', None)
        if dt is None:
            return 0
        try:
            return dt.timestamp()
        except Exception:
            return 0

    threads_list.sort(key=_key_created)

    # Keep closed threads behavior (locked & not archived) as in debugthreads, and also list open threads
    # Step 1: Put all threads into the 'Fermés' section (user request)
    closed_threads = threads_list
    open_threads = []

    lines = []
    # Closed section will contain all threads; remove forum segment per user request
    lines.append(f"🧵 Fermés ({len(closed_threads)}):")
    for t in closed_threads:
        created = t.created_at.strftime('%d/%m/%Y %H:%M') if getattr(t, 'created_at', None) else '?'

        # Passe juste l'id, pas l'objet
        lock_date = await get_lock_date(t.id, ctx.guild)
        locked = lock_date.strftime('%d/%m/%Y %H:%M') if lock_date else '—'

        # Date programmée de suppression = date de fermeture + 1 semaine
        scheduled_deletion = None
        if lock_date:
            try:
                scheduled_deletion = lock_date + timedelta(weeks=1)
            except Exception:
                scheduled_deletion = None

        scheduled_str = scheduled_deletion.strftime('%d/%m/%Y %H:%M') if scheduled_deletion else '—'

        lines.append(f"• {t.name} — id:{t.id} — créé:{created} — fermé:{locked} — suppression prévue:{scheduled_str}")

    msg = "\n".join(lines)
    # send in chunks to avoid message length limits — split only at line boundaries
    for chunk in _chunks_from_lines(msg):
        await ctx.send(chunk)


@admin.command(name="openthreads")
@commands.has_permissions(administrator=True)
async def admin_openthreads(ctx):
    """Liste uniquement les posts ouverts (non fermés ET non archivés)."""
    guild = ctx.guild
    open_threads = []

    for channel in guild.channels:
        if isinstance(channel, discord.ForumChannel):
            try:
                # Threads actifs dans le cache
                for t in channel.threads:
                    if not getattr(t, 'locked', False) and not getattr(t, 'archived', False):
                        if not getattr(t, 'parent', None):
                            t.parent = channel
                        open_threads.append(t)
            except Exception as e:
                await ctx.send(f"⚠️ Erreur forum {channel.name}: {e}")
                continue

    if not open_threads:
        return await ctx.send("🔓 Aucun post ouvert trouvé sur ce serveur.")

    # Tri par date
    open_threads.sort(key=lambda t: t.created_at or 0)

    # Message
    lines = [f"🔓 Posts ouverts ({len(open_threads)}):"]
    for t in open_threads:
        created = t.created_at.strftime('%d/%m/%Y %H:%M') if t.created_at else '?'
        lines.append(f"• {t.name} — id:{t.id} — créé:{created}")

    msg = "\n".join(lines)
    for chunk in _chunks_from_lines(msg):
        await ctx.send(chunk)

@admin.command(name="listthreads")
@commands.has_permissions(administrator=True)
async def admin_listthreads(ctx):
    """Liste tous les posts de chaque forum, groupés par statut (ouvert, fermé)."""
    # Reuse existing admin commands to ensure identical output and formatting.
    # User requested: first show open threads, then closed threads.
    await admin_openthreads(ctx)
    await admin_debugthreads(ctx)

@bot.command(name="close")
async def close_thread(ctx, post_id: int = None):
    """Ferme (archive) un post de forum.

    Usage:
      - `!close` (dans un post) — archive le post courant
      - `!close <post_id>` (admin) — archive le post avec l'ID fourni
    """
    thread = None

    # If an ID is provided, search all forum channels for that thread
    if post_id is not None:
        for channel in ctx.guild.channels:
            if isinstance(channel, discord.ForumChannel):
                try:
                    for t in channel.threads:
                        if t.id == post_id:
                            thread = t
                            break
                except Exception:
                    continue
            if thread:
                break

        if not thread:
            await ctx.send(f"❌ Post {post_id} introuvable.")
            return
    else:
        # No ID: must be used inside a thread
        if isinstance(ctx.channel, discord.Thread):
            thread = ctx.channel
        else:
            await ctx.send("⚠️ Cette commande doit être utilisée dans un post de forum ou avec un ID : `!close <post_id>`.")
            return

    # If already archived, inform and return
    try:
        if getattr(thread, 'archived', False):
            await ctx.send(f"ℹ️ Le post {thread.id} est déjà archivé.")
            return
    except Exception:
        pass

    # Permission check: Manage Threads is required to archive
    perms = None
    try:
        perms = thread.permissions_for(ctx.guild.me)
    except Exception:
        perms = None

    if perms is not None and not getattr(perms, 'manage_threads', False):
        await ctx.send("❌ Je n'ai pas la permission `Manage Threads` pour archiver ce post. Vérifie mes permissions.")
        return

    # Attempt to archive using the library, then verify. If it doesn't take effect, try REST fallback.
    try:
        await thread.edit(archived=True)

        # verify by fetching fresh channel object
        try:
            refreshed = await thread.guild.fetch_channel(thread.id)
            archived_now = getattr(refreshed, 'archived', False)
        except Exception:
            refreshed = None
            archived_now = None

        # If the library call didn't actually archive, try REST fallback (requires BOT token)
        if not archived_now:
            fallback_ok = False
            if TOKEN:
                try:
                    url = f"https://discord.com/api/v10/channels/{thread.id}"
                    headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
                    payload = {"archived": True}
                    resp = requests.patch(url, headers=headers, json=payload, timeout=10)
                    if resp.status_code in (200, 201):
                        fallback_ok = True
                    else:
                        logger.warning(f"REST fallback archive failed for {thread.id}: {resp.status_code} {resp.text}")
                except Exception as e:
                    logger.warning(f"REST fallback archive exception for {thread.id}: {e}")

            # re-fetch to confirm
            try:
                refreshed = await thread.guild.fetch_channel(thread.id)
                archived_now = getattr(refreshed, 'archived', False)
            except Exception:
                archived_now = False

        if archived_now:
            now_dt = datetime.now(timezone.utc)
            try:
                closed_threads[str(thread.id)] = now_dt.isoformat()
                closing_cache[thread.id] = now_dt
                save_closed_threads()
            except Exception as _e:
                logger.warning(f"Impossible d'enregistrer la fermeture du thread {thread.id}: {_e}")

            # Notify the user outside of the (now archived) thread to avoid unarchiving it
            msg = f"✅ Post {thread.id} archivé (clos) avec succès."
            sent = await send_confirmation_outside_thread(ctx, thread, msg)
            if not sent:
                # last fallback: log if we couldn't send anywhere
                logger.info(msg)
            print(f"🧵 Post '{thread.name}' ({thread.id}) archivé manuellement par {ctx.author}.")
            return

        # If we reach here, archive did not succeed
        await ctx.send("❌ Tentative d'archivage effectuée mais le post reste ouvert. Vérifie mes permissions et le type de thread (public/private).")
        logger.warning(f"Archive reported success but channel {thread.id} not archived (checked properties).")

    except discord.Forbidden:
        await ctx.send("❌ Je n'ai pas la permission d'archiver ce post. Vérifie `Manage Threads` et les permissions de canal.")
    except Exception as e:
        await ctx.send(f"❌ Erreur lors de l'archivage: {e}")


# ========== To fix ===========

# @tasks.loop(minutes=1)
# async def check_meetings():
#     """Vérifie les événements Discord planifiés et envoie un rappel 5 min avant aux intéressés non connectés"""
#     now = datetime.now(timezone.utc)
#     for guild in bot.guilds:
#         events = await guild.fetch_scheduled_events()
#         for event in events:
#             # Inspect event and compute start delta
#             start_time = get_event_start_time(event)
#             logger.debug(f"Checking event {getattr(event,'name','N/A')} (id={getattr(event,'id','N/A')}), status={getattr(event,'status','N/A')}, start_time={start_time}")
#             if event.status != discord.EventStatus.scheduled:
#                 logger.debug(f"Skipping event {getattr(event,'id','N/A')} because status != scheduled ({getattr(event,'status','N/A')})")
#                 continue
#             if start_time is None:
#                 logger.debug(f"Skipping event {getattr(event,'id','N/A')} because start_time is None")
#                 continue
#             delta = (start_time - now).total_seconds()
#             logger.debug(f"Event delta (seconds) for {getattr(event,'id','N/A')}: {delta}")

#             # Si l’événement commence dans 5 minutes ou moins
#             if 0 < delta <= 300:
#                 # Resolve the reminder channel (may use per-guild override)
#                 channel = get_reminder_channel(guild, event)
#                 if not channel or not hasattr(channel, 'send'):
#                     logger.error(f"⚠️ Aucun channel textuel disponible pour envoyer le rappel de {getattr(event,'name','N/A')} (guild {guild.id}).")
#                     continue

#                 # 🔹 Étape 1 — Récupérer les personnes intéressées (compatibilité versions discord.py)
#                 interested_users = await get_event_interested_users(guild, event)
#                 logger.debug(f"Retrieved {len(interested_users) if interested_users is not None else 0} interested users for event {getattr(event,'id','N/A')}")

#                 # 🔹 Étape 2 — Identifier qui est déjà dans le salon vocal
#                 already_connected = []
#                 if isinstance(event.channel, discord.VoiceChannel):
#                     already_connected = [m for m in event.channel.members]

#                 # 🔹 Étape 3 — Filtrer pour ne pinguer que ceux pas encore connectés et qui veulent des notifications
#                 users_to_ping = [
#                     u.mention for u in interested_users
#                     if getattr(u, 'id', None) not in notify_opt_out and all(getattr(u,'id', None) != m.id for m in already_connected)
#                 ]

#                 if not users_to_ping:
#                     logger.info(f"Personne à ping pour {getattr(event,'name','N/A')} (tous déjà connectés ou opt-out 👏)")
#                     continue

#                 mentions = ", ".join(users_to_ping)

#                 embed = discord.Embed(
#                     title=f"⏰ Rappel : {event.name}",
#                     description=f"L’événement commence dans **5 minutes** !\n\n🔔 Participants à prévenir : {mentions}",
#                     color=0x5865F2,
#                     timestamp=start_time
#                 )
#                 # Indiquer dans le footer le channel ciblé (sera utile pour retrouver le message)
#                 target_channel_name = None
#                 if channel is not None:
#                     target_channel_name = getattr(channel, 'name', None) or str(getattr(channel, 'id', 'N/A'))
#                 footer_text = f"Heure locale selon le fuseau horaire Discord de chacun. | channel: {target_channel_name or 'unknown'}"
#                 embed.set_footer(text=footer_text)

#                 try:
#                     # First send plain mentions to trigger pings
#                     allowed_ping = discord.AllowedMentions(users=True)
#                     try:
#                         await channel.send(mentions, allowed_mentions=allowed_ping)
#                     except Exception:
#                         logger.warning(f"Envoi du contenu de mentions échoué pour {event.name} dans {getattr(channel,'name', getattr(channel,'id','N/A'))}")

#                     # Then send the embed without mentions to avoid double pings
#                     try:
#                         await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
#                     except Exception as e:
#                         logger.error(f"Erreur envoi embed rappel pour {event.name} dans {getattr(channel,'name', getattr(channel,'id','N/A'))}: {e}")
#                         raise

#                     ch_desc = getattr(channel, 'name', None) or getattr(channel, 'id', 'N/A')
#                     logger.info(f"🔔 Rappel envoyé pour {event.name} (ping de {len(users_to_ping)} membres) dans channel '{ch_desc}'")
#                 except Exception as e:
#                     ch_desc = getattr(channel, 'name', None) or getattr(channel, 'id', 'N/A')
#                     logger.error(f"⚠️ Échec envoi du rappel pour {event.name} dans channel '{ch_desc}': {e}")
#                     # try to fallback to system channel if available and different
#                     try:
#                         if guild.system_channel and getattr(guild.system_channel, 'send', None) and guild.system_channel != channel:
#                             allowed_ping = discord.AllowedMentions(users=True)
#                             try:
#                                 await guild.system_channel.send(mentions, allowed_mentions=allowed_ping)
#                             except Exception:
#                                 logger.warning(f"Envoi du contenu de mentions échoué pour {event.name} dans system_channel")
#                             await guild.system_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
#                             sys_desc = getattr(guild.system_channel, 'name', None) or getattr(guild.system_channel, 'id', 'N/A')
#                             logger.info(f"🔔 Rappel envoyé pour {event.name} dans system_channel '{sys_desc}'")
#                     except Exception as e2:
#                         logger.error(f"⚠️ Échec envoi du rappel fallback pour {event.name}: {e2}")

#                 # Évite le spam toutes les minutes
#                 await asyncio.sleep(65)


# ============ LANCEMENT DU BOT ============

if __name__ == "__main__":
    bot.run(TOKEN)
