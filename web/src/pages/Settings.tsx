import { useEffect, useState } from "react";
import { api } from "../api";
import { useLive } from "../live";
import { SteamIdList } from "../steam";
import { useUnsaved } from "../unsaved";
import { UpnpRouter } from "../upnp";
import { Button, Card, Field, Grid, Input, LockNotice, PageHeader, Select, Toggle, useConfig, useLoad, useLocked, useToast } from "../ui";

interface GameSettings {
  server_name: string;
  description: string;
  password: string;
  http_port: number;
  super_admins: string[];
  admins: string[];
}

const MIN_CHECK_MINUTES = 10;
const MAX_CHECK_MINUTES = 24 * 60;

/** How often to look for updates: a number with a minutes/hours choice, stored in minutes (10 minutes to 24 hours). */
function CheckInterval({ minutes, disabled, onChange }: { minutes: number; disabled: boolean; onChange: (minutes: number) => void }) {
  // Whole hours read as hours; anything else as minutes. The choice then stays where the person put it.
  const [unit, setUnit] = useState<"minutes" | "hours">(minutes % 60 === 0 ? "hours" : "minutes");
  const per = unit === "hours" ? 60 : 1;
  const show = (value: number) => String(unit === "hours" ? Math.round((value / 60) * 100) / 100 : value);
  // What is in the box while typing. It is only brought within the limits on leaving the box:
  // correcting "4" to 10 on the way to "45" would make the number impossible to type.
  const [text, setText] = useState(show(minutes));
  const clamp = (value: number) => Math.min(MAX_CHECK_MINUTES, Math.max(MIN_CHECK_MINUTES, Math.round(value)));
  useEffect(() => {
    if (Math.round(Number(text) * per) !== minutes) setText(show(minutes)); // saved, reloaded or the unit changed
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [minutes, unit]);
  return (
    <Field label="Check for updates every" hint="For both the manager and the game server: from 10 minutes to 24 hours.">
      <div className="flex max-w-64 gap-2">
        <Input
          type="number"
          min={unit === "hours" ? 1 : MIN_CHECK_MINUTES}
          max={unit === "hours" ? 24 : MAX_CHECK_MINUTES}
          step={1}
          value={text}
          disabled={disabled}
          aria-label="How often"
          onChange={(e) => {
            setText(e.target.value);
            const value = Math.round(Number(e.target.value) * per);
            if (e.target.value !== "" && value >= MIN_CHECK_MINUTES && value <= MAX_CHECK_MINUTES) onChange(value);
          }}
          onBlur={() => {
            const value = clamp((Number(text) || 0) * per);
            onChange(value);
            setText(show(value));
          }}
        />
        <Select
          value={unit}
          disabled={disabled}
          aria-label="Minutes or hours"
          onChange={(e) => {
            const next = e.target.value as "minutes" | "hours";
            // Keep the number and change what it means (30 minutes becomes 30 hours), then bring it back within the limits.
            onChange(clamp((Number(text) || 0) * (next === "hours" ? 60 : 1)));
            setUnit(next);
          }}
        >
          <option value="minutes">minutes</option>
          <option value="hours">hours</option>
        </Select>
      </div>
    </Field>
  );
}

export default function Settings() {
  const { cfg, set, save, dirty: cfgDirty } = useConfig();
  const { data: game, setData: setGame, setSaved: setSavedGame, dirty: gameDirty } = useLoad<GameSettings>("/game-settings");
  const locked = useLocked();
  const toast = useToast();
  const { status } = useLive();
  const [saving, setSaving] = useState(false);
  const saveAll = async () => {
    if (!game) return false;
    setSaving(true);
    try {
      if (!locked) setSavedGame(await api<GameSettings>("/game-settings", { method: "PUT", body: game }));
      return await save("launch", "rcon", "behavior", "network");
    } catch (e) {
      toast.err(e);
      return false;
    } finally {
      setSaving(false);
    }
  };
  useUnsaved(cfgDirty || gameDirty, saveAll);
  if (!cfg || !game) return null;

  const g = <K extends keyof GameSettings>(key: K, value: GameSettings[K]) => setGame({ ...game, [key]: value });
  const num = (v: string) => (v === "" ? 0 : Number(v));

  return (
    <>
      <PageHeader
        title="Server Settings"
        desc="Identity, network and launch options."
        actions={
          <Button variant="primary" onClick={saveAll} disabled={saving}>
            Save changes
          </Button>
        }
      />
      <LockNotice />
      <div className="space-y-4">
        <Card title="Identity" desc="Stored in Game.ini.">
          <fieldset disabled={locked} className="space-y-4">
            <Grid>
              <Field label="Server name">
                <Input value={game.server_name} onChange={(e) => g("server_name", e.target.value)} maxLength={128} />
              </Field>
              <Field label="Join password" hint="Leave empty for a public server. Letters, numbers, - and _ only.">
                <Input value={game.password} onChange={(e) => g("password", e.target.value)} autoComplete="off" />
              </Field>
            </Grid>
            <Field label="Description">
              <Input value={game.description} onChange={(e) => g("description", e.target.value)} maxLength={512} />
            </Field>
            <Grid>
              <Field label="Super admins" hint="Full control in game, including making other players admins.">
                <SteamIdList ids={game.super_admins} onChange={(ids) => g("super_admins", ids)} addLabel="Add super admin" pickerTitle="Add a super admin" empty="No super admins." />
              </Field>
              <Field label="Admins">
                <SteamIdList ids={game.admins} onChange={(ids) => g("admins", ids)} addLabel="Add admin" pickerTitle="Add an admin" empty="No admins." />
              </Field>
            </Grid>
            <Field label="Game HTTP API port" hint="0 disables it. The game's API has no authentication: never expose this port to the internet.">
              <Input type="number" min={0} max={65535} value={game.http_port} onChange={(e) => g("http_port", num(e.target.value))} className="max-w-40" />
            </Field>
          </fieldset>
        </Card>

        <Card title="Network & launch" desc="Applied the next time the server starts.">
          <div className="space-y-4">
            <Grid>
              <Field label="Game port (UDP)">
                <Input type="number" min={1} max={65535} value={cfg.launch.port} onChange={(e) => set("launch", "port", num(e.target.value))} />
              </Field>
              <Field label="Query port (UDP)">
                <Input type="number" min={1} max={65535} value={cfg.launch.query_port} onChange={(e) => set("launch", "query_port", num(e.target.value))} />
              </Field>
              <Field label="Max players">
                <Input type="number" min={1} value={cfg.launch.max_players} onChange={(e) => set("launch", "max_players", num(e.target.value))} />
              </Field>
              <Field label="Session name">
                <Input value={cfg.launch.session_name} onChange={(e) => set("launch", "session_name", e.target.value)} />
              </Field>
            </Grid>
            <Field label="Map">
              <Input value={cfg.launch.map} onChange={(e) => set("launch", "map", e.target.value)} />
            </Field>
            <Field label="Extra launch arguments" hint="Appended to the command line, e.g. -multihome=0.0.0.0">
              <Input value={cfg.launch.extra_args} onChange={(e) => set("launch", "extra_args", e.target.value)} />
            </Field>
            <Toggle
              label="Run the game server on a single thread"
              hint="For when several dedicated servers share this machine (-onethread). Leave off for one server."
              checked={!!cfg.launch.single_thread}
              onChange={(v) => set("launch", "single_thread", v)}
            />
          </div>
        </Card>

        <Card title="Router port forwarding" desc="Players outside your network reach the server through your router.">
          {status?.capabilities.upnp ? (
            <div className="space-y-4">
              <Toggle
                label="Ask my router to forward the game ports (UPnP)"
                hint="Forwards only the two game ports above, only while the server runs. Applies the next time the server starts. Results are shown under System > Health check."
                checked={cfg.network.upnp}
                onChange={(v) => set("network", "upnp", v)}
              />
              {cfg.network.upnp && <UpnpRouter value={cfg.network.upnp_adapter ?? ""} onChange={(v) => set("network", "upnp_adapter", v)} />}
            </div>
          ) : (
            <p className="text-sm text-zinc-400">Not available inside Docker. Forward both UDP ports on your router yourself.</p>
          )}
        </Card>

        <Card title="Behaviour">
          <div className="grid gap-4 sm:grid-cols-2">
            <Toggle label="Start server with the manager" hint="Launch the game server whenever the manager (or container) starts, for example after Windows restarts. Not if you last stopped it yourself: then it stays stopped until you start it." checked={cfg.behavior.auto_start} onChange={(v) => set("behavior", "auto_start", v)} />
            <Toggle label="Restart after a crash" hint="Stops trying after 5 crashes in 10 minutes." checked={cfg.behavior.keep_alive} onChange={(v) => set("behavior", "keep_alive", v)} />
            <Toggle label="Check for manager updates" hint="Asks GitHub whether a newer release exists. Nothing is downloaded automatically." checked={cfg.behavior.update_check} onChange={(v) => set("behavior", "update_check", v)} />
            <Toggle
              label="Include beta versions"
              hint="Also tell me about pre-release builds."
              checked={cfg.behavior.update_prereleases}
              disabled={!cfg.behavior.update_check}
              onChange={(v) => set("behavior", "update_prereleases", v)}
            />
            <Toggle
              label="Check for game server updates"
              hint="Asks Steam for the current build of your branch and tells you when it differs from the one installed."
              checked={cfg.behavior.game_update_check}
              onChange={(v) => set("behavior", "game_update_check", v)}
            />
            <Toggle
              label="Install game updates when nobody is online"
              hint="Stops the server, updates and starts it again, only while it is online and empty."
              checked={cfg.behavior.game_update_auto}
              disabled={!cfg.behavior.game_update_check}
              onChange={(v) => set("behavior", "game_update_auto", v)}
            />
            <CheckInterval minutes={cfg.behavior.update_check_minutes} disabled={!cfg.behavior.update_check && !cfg.behavior.game_update_check} onChange={(v) => set("behavior", "update_check_minutes", v)} />
          </div>
        </Card>

        <Card title="RCON">
          <div className="space-y-4">
            <Toggle label="Enable RCON" checked={cfg.rcon.enabled} onChange={(v) => set("rcon", "enabled", v)} />
            <Grid>
              <Field label="RCON port">
                <Input type="number" min={1} max={65535} value={cfg.rcon.port} onChange={(e) => set("rcon", "port", num(e.target.value))} disabled={!cfg.rcon.enabled} />
              </Field>
              <Field label="RCON password">
                <Input type="password" autoComplete="new-password" value={cfg.rcon.password} onChange={(e) => set("rcon", "password", e.target.value)} disabled={!cfg.rcon.enabled} />
              </Field>
            </Grid>
          </div>
        </Card>
      </div>
    </>
  );
}
