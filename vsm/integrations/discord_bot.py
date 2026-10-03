"""Discord integration: one-way webhooks and the optional two-way command bot."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import urllib.request
from datetime import datetime, timezone

from .. import VsmError, __version__
from ..core.network import public_ip

log = logging.getLogger("vsm.discord")

try:  # optional dependency: pip install vein-server-manager[discord]
    import discord
except ImportError:
    discord = None

COLORS = {"START": 5763719, "STOP": 15548997, "CRASH": 15158332, "UPDATE": 3447003, "WARN": 16776960}


class Discord:
    def __init__(self, manager):
        self.m = manager

    # ---------- webhooks ----------

    def _post(self, embed: dict) -> None:
        cfg = self.m.config.get().discord
        if not (cfg.webhook_enabled and cfg.webhook_url):
            return
        embed["timestamp"] = datetime.now(timezone.utc).isoformat()
        body = json.dumps({"embeds": [embed]}).encode()

        def send():
            try:
                req = urllib.request.Request(
                    cfg.webhook_url, data=body, headers={"Content-Type": "application/json", "User-Agent": "VeinManager"}
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                log.warning("webhook failed: %s", e)

        threading.Thread(target=send, daemon=True).start()

    def notify(self, kind: str, description: str) -> None:
        self._post({
            "title": f"Vein Server - {kind}",
            "description": description,
            "color": COLORS.get(kind, 0),
            "footer": {"text": f"Vein Server Manager v{__version__}"},
        })

    def chat(self, description: str, color: int) -> None:
        self._post({"description": description, "color": color})

    # ---------- bot ----------

    def start_bot(self) -> None:
        cfg = self.m.config.get().discord
        if not (cfg.bot_enabled and cfg.bot_token):
            return
        if discord is None:
            self.m.event("DISCORD", "Bot is enabled but discord.py is not installed (pip install discord.py).")
            return

        def run():
            try:
                _build_bot(self).run(cfg.bot_token, log_handler=None)
            except Exception as e:
                self.m.event("DISCORD", f"Bot stopped: {e}")

        threading.Thread(target=run, name="discord-bot", daemon=True).start()

    def status_embed(self):
        s = self.m.server.status()
        online = s["state"] == "ONLINE"
        embed = discord.Embed(title="Server Live Status", color=0x00FF00 if online else 0xFF0000)
        embed.add_field(name="Status", value="🟢 ONLINE" if online else f"🔴 {s['state']}", inline=True)
        embed.add_field(name="IP Address", value=f"`{public_ip()}`", inline=True)
        hours, rem = divmod(s["uptime"], 3600)
        embed.add_field(name="Uptime", value=f"{hours}h {rem // 60}m" if s["pid"] else "N/A", inline=True)
        embed.add_field(name="RAM Usage", value=f"{s['ram_mb']} MB", inline=True)
        names = "\n".join(f"• {p['name']}" for p in s["players"])
        if len(names) > 1000:
            names = names[:900] + "\n...and more"
        embed.add_field(name=f"Online Players ({len(s['players'])})", value=names or "No players online.", inline=False)
        embed.set_footer(text=f"Last updated {datetime.now().strftime('%H:%M:%S')}")
        return embed


def _build_bot(integration: Discord):
    m = integration.m
    cfg = m.config.get().discord
    channel_id = int(cfg.channel_id) if cfg.channel_id else 0
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    state = {"message_id": int(cfg.live_status_message_id) if cfg.live_status_message_id else None, "loop": False}

    async def update_board():
        channel = client.get_channel(channel_id)
        if not channel:
            return
        embed = await asyncio.to_thread(integration.status_embed)
        if state["message_id"]:
            try:
                msg = await channel.fetch_message(state["message_id"])
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                state["message_id"] = None  # deleted by hand; post a new one
        msg = await channel.send(embed=embed)
        try:
            await msg.pin()
        except discord.DiscordException:
            pass
        state["message_id"] = msg.id
        m.config.update({"discord": {"live_status_message_id": str(msg.id)}})

    async def board_loop():
        while not client.is_closed():
            try:
                await update_board()
            except Exception as e:
                log.warning("live status update failed: %s", e)
            await asyncio.sleep(60)

    @client.event
    async def on_ready():
        m.event("DISCORD", f"Bot connected as {client.user}.")
        if channel_id and not state["loop"]:
            state["loop"] = True
            client.loop.create_task(board_loop())

    @client.event
    async def on_message(message):
        # Commands are only honoured in the configured channel.
        if message.author == client.user or not channel_id or message.channel.id != channel_id:
            return
        command = message.content.lower().strip()
        actions = {
            "!start": ("🚀 **Start command received.** Launching server...", lambda: m.server.request_start("DISCORD")),
            "!stop": ("🛑 **Stop command received.** Shutting down...", lambda: m.server.request_stop("DISCORD")),
            "!restart": ("🔄 **Restart command received.**", lambda: m.server.request_restart("DISCORD")),
            "!backup": ("💾 **Backup created.**", lambda: m.backups.create("DISCORD")),
        }
        if command == "!status":
            await update_board()
        elif command == "!ip":
            ip = await asyncio.to_thread(public_ip)
            await message.channel.send(f"🌐 **Public IP:** `{ip}`")
        elif command in actions:
            reply, action = actions[command]
            try:
                await asyncio.to_thread(action)
                await message.channel.send(reply)
            except VsmError as e:
                await message.channel.send(f"⚠️ {e}")

    return client
