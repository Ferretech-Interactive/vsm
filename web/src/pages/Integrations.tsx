import { api } from "../api";
import { Button, Card, Field, Grid, Input, PageHeader, Toggle, useConfig, useLoad, useLocked, useToast } from "../ui";
import { useUnsaved } from "../unsaved";

/** The two addresses the game itself posts to; they live in Game.ini with the other game settings. */
interface GameRelay {
  discord_chat_webhook: string;
  discord_admin_webhook: string;
  [other: string]: unknown;
}

export default function Integrations() {
  const { cfg, set, save, dirty } = useConfig();
  const { data: game, setData: setGame, setSaved: setSavedGame, dirty: gameDirty } = useLoad<GameRelay>("/game-settings");
  const locked = useLocked();
  const toast = useToast();
  const saveAll = async () => {
    if (game && gameDirty) {
      try {
        const { discord_chat_webhook, discord_admin_webhook } = game;
        setSavedGame(await api<GameRelay>("/game-settings", { method: "PUT", body: { discord_chat_webhook, discord_admin_webhook } }));
      } catch (e) {
        toast.err(e);
        return false;
      }
    }
    return save("discord");
  };
  useUnsaved(dirty || gameDirty, saveAll);
  if (!cfg || !game) return null;
  const d = cfg.discord;
  return (
    <>
      <PageHeader
        title="Integrations"
        desc="Discord notifications and remote control."
        actions={
          <Button variant="primary" onClick={saveAll}>
            Save changes
          </Button>
        }
      />
      <div className="space-y-4">
        <Card title="Discord webhook" desc="One-way: start, stop, crash and restart warnings, plus player joins, chat and kills.">
          <div className="space-y-4">
            <Toggle label="Send events to a webhook" checked={d.webhook_enabled} onChange={(v) => set("discord", "webhook_enabled", v)} />
            <Field label="Webhook URL" hint="Channel settings → Integrations → Webhooks. Shown masked once saved.">
              <Input type="password" autoComplete="off" value={d.webhook_url} onChange={(e) => set("discord", "webhook_url", e.target.value)} placeholder="https://discord.com/api/webhooks/…" />
            </Field>
          </div>
        </Card>

        <Card title="In-game chat to Discord" desc="Built into the game: it posts global chat, and reports made to admins, straight to Discord. Stored in Game.ini.">
          <fieldset disabled={locked} className="space-y-4">
            <Grid>
              <Field label="Global chat webhook URL" hint="Every global chat message is posted here.">
                <Input type="password" autoComplete="off" value={game.discord_chat_webhook} onChange={(e) => setGame({ ...game, discord_chat_webhook: e.target.value })} placeholder="https://discord.com/api/webhooks/…" />
              </Field>
              <Field label="Admin reports webhook URL" hint="Player reports go here; use a staff-only channel.">
                <Input type="password" autoComplete="off" value={game.discord_admin_webhook} onChange={(e) => setGame({ ...game, discord_admin_webhook: e.target.value })} placeholder="https://discord.com/api/webhooks/…" />
              </Field>
            </Grid>
            {locked && <p className="text-xs text-amber-300">Stop the server to change these: it rewrites Game.ini when it exits.</p>}
          </fieldset>
        </Card>

        <Card title="Discord bot" desc="Two-way: a pinned live status board and the commands !status, !start, !stop, !restart, !backup, !ip.">
          <div className="space-y-4">
            <Toggle label="Run the bot" hint="Changes take effect after the manager restarts." checked={d.bot_enabled} onChange={(v) => set("discord", "bot_enabled", v)} />
            <Grid>
              <Field label="Bot token">
                <Input type="password" autoComplete="off" value={d.bot_token} onChange={(e) => set("discord", "bot_token", e.target.value)} />
              </Field>
              <Field label="Channel ID" hint="Commands are only accepted in this channel.">
                <Input value={d.channel_id} onChange={(e) => set("discord", "channel_id", e.target.value)} inputMode="numeric" className="font-mono" />
              </Field>
              <Field
                label="Roles that may control the server"
                hint="Role IDs, comma separated. !start, !stop, !restart and !backup work for members who can manage the Discord server, and for these roles. !status and !ip work for anyone in the channel."
              >
                <Input value={d.command_roles} onChange={(e) => set("discord", "command_roles", e.target.value)} inputMode="numeric" className="font-mono" placeholder="Optional" />
              </Field>
            </Grid>
            <ol className="list-decimal space-y-1 pl-5 text-xs text-zinc-400">
              <li>discord.com/developers/applications → New Application → Bot → enable “Message Content Intent”.</li>
              <li>Reset Token, copy it and paste it above.</li>
              <li>OAuth2 → URL Generator → scope “bot” with Send Messages, Manage Messages and Read Message History, then open the URL to invite it.</li>
              <li>In Discord, enable Developer Mode, right-click the channel and Copy Channel ID. Role IDs are copied from Server Settings → Roles.</li>
            </ol>
          </div>
        </Card>
      </div>
    </>
  );
}
