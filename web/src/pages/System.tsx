import { useState, type ReactNode } from "react";
import { api } from "../api";
import { useLive } from "../live";
import { PortCheck } from "../portcheck";
import { Badge, Button, Card, Empty, Field, Grid, Input, PageHeader, Toggle, useConfig, useLoad, useLocked, useToast } from "../ui";

/** Who holds a UDP port on the server's own machine. */
interface PortUse {
  state: "game" | "other" | "free" | null;
  program: string;
}

interface Diagnostics {
  public_ip: string;
  game_port: number;
  game_port_use: PortUse;
  query_port: number;
  query_port_use: PortUse;
  firewall: { managed: boolean; state: "ok" | "mismatch" | "missing" | "unmanaged" | null; ports: number[] };
  upnp: { state: "off" | "mapped" | "error" | "unavailable"; message: string; external_ip: string; ports: number[] };
  game_log: { log_found: boolean; findings: { id: string; level: "ok" | "info" | "warn" | "error"; title: string; detail: string }[] };
  disk_free_gb: number;
  disk_total_gb: number;
  data_dir: string;
  server_dir: string;
  in_container: boolean;
}

function Health() {
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const { status } = useLive();
  const { cfg } = useConfig();
  const run = () => {
    setBusy(true);
    api<Diagnostics>("/diagnostics")
      .then(setDiag)
      .catch(toast.err)
      .finally(() => setBusy(false));
  };
  const port = (use: PortUse) => {
    if (use.state === null) return <Badge>Unknown</Badge>;
    if (use.state === "game") return <Badge tone="green">Game server is listening</Badge>;
    if (use.state === "other")
      return (
        <span>
          <Badge tone="red">In use by {use.program || "another program"}</Badge>
          <span className="mt-1 block text-xs text-zinc-400">The game server cannot use this port while that program has it. Close it, or choose another port under Server Settings.</span>
        </span>
      );
    return status?.state === "ONLINE" ? <Badge tone="red">Game server is not listening</Badge> : <Badge>Free</Badge>;
  };
  const Group = ({ title, note }: { title: string; note: string }) => (
    <div className="mb-1">
      <h3 className="text-xs font-semibold tracking-wide text-zinc-300 uppercase">{title}</h3>
      <p className="mt-0.5 text-xs text-zinc-500">{note}</p>
    </div>
  );
  const Row = ({ label, children }: { label: string; children: ReactNode }) => (
    <div className="flex items-baseline gap-3 py-1.5 text-sm">
      <span className="w-36 shrink-0 text-zinc-400">{label}</span>
      <span className="min-w-0 flex-1 text-zinc-200">{children}</span>
    </div>
  );
  return (
    <Card
      title="Health check"
      actions={
        <Button onClick={run} disabled={busy}>
          {busy ? "Checking…" : "Run health check"}
        </Button>
      }
    >
      {!diag ? (
        <Empty>Checks this machine (ports, its firewall, disk space, the game log), then shows what stands between it and players, with a button to test the ports from the internet.</Empty>
      ) : (
        <div className="grid gap-x-8 gap-y-5 xl:grid-cols-2">
          <section className="min-w-0">
            <Group title="This machine" note="Checked on the server's own PC only. Green here does not mean players can reach it." />
            <div className="divide-y divide-zinc-800">
          <Row label={`Game port ${diag.game_port}/udp`}>{port(diag.game_port_use)}</Row>
          <Row label={`Query port ${diag.query_port}/udp`}>{port(diag.query_port_use)}</Row>
          <Row label={diag.in_container || diag.firewall.state === null ? "Firewall" : "Windows Firewall"}>
            {diag.in_container ? (
              <span className="text-xs text-zinc-400">Publish both UDP ports from the container and open them on the host.</span>
            ) : diag.firewall.state === null ? (
              <span className="text-xs text-zinc-400">Make sure both UDP ports are allowed (ufw / firewalld / cloud firewall).</span>
            ) : diag.firewall.state === "unmanaged" ? (
              <span className="text-xs text-zinc-400">Not managed in this mode: allow both UDP ports in Windows Firewall yourself.</span>
            ) : diag.firewall.state === "ok" ? (
              <Badge tone="green">Rule matches the game ports</Badge>
            ) : diag.firewall.state === "missing" ? (
              <Badge tone="red">No rule: players cannot connect. Run Setup again to repair it.</Badge>
            ) : (
              <Badge tone="amber">Rule is for {diag.firewall.ports.join(", ")}; it updates within a few seconds of a port change</Badge>
            )}
          </Row>
          {!diag.game_log.log_found ? (
            <Row label="Game log">
              <span className="text-xs text-zinc-400">No game log yet: it is checked for known problems once the server has run.</span>
            </Row>
          ) : (
            diag.game_log.findings.map((f) => (
              <Row key={f.id} label="Game log">
                <span>
                  <Badge tone={f.level === "ok" ? "green" : f.level === "error" ? "red" : f.level === "warn" ? "amber" : "blue"}>{f.title}</Badge>
                  {f.detail && <span className="mt-1 block text-xs text-zinc-400">{f.detail}</span>}
                </span>
              </Row>
            ))
          )}
          <Row label="Disk free">
            {diag.disk_free_gb} GB of {diag.disk_total_gb} GB
          </Row>
          <Row label="Data folder">
            <span className="font-mono text-xs break-all">{diag.data_dir}</span>
          </Row>
          <Row label="Server folder">
            <span className="font-mono text-xs break-all">{diag.server_dir}</span>
          </Row>
            </div>
          </section>
          <section className="min-w-0">
            <Group title="Your network and the internet" note="What stands between this machine and players elsewhere." />
            <div className="divide-y divide-zinc-800">
          <Row label="Public IP">
            <span className="font-mono">{diag.public_ip}</span>
          </Row>
          <Row label="Router (UPnP)">
            {diag.upnp.state === "mapped" ? (
              <span>
                <Badge tone="green">Forwarding {diag.upnp.ports.join(", ")}</Badge>
                <span className="mt-1 block text-xs text-zinc-400">{diag.upnp.message}</span>
              </span>
            ) : diag.upnp.state === "error" ? (
              <span className="text-xs text-red-300">{diag.upnp.message}</span>
            ) : (
              <span className="text-xs text-zinc-400">{diag.upnp.message || "Off. Turn it on under Server Settings, or forward the ports on your router."}</span>
            )}
          </Row>
          {cfg && !diag.in_container && (
            <div className="pt-3">
              <PortCheck
                inSetup={false}
                port={cfg.launch.port}
                queryPort={cfg.launch.query_port}
                upnp={!!cfg.network.upnp && !!status?.capabilities.upnp}
                probeUrl={cfg.network.probe_url ?? ""}
                windows={status?.platform === "windows"}
                disabled={status && (status.state !== "OFFLINE" || status.task) ? "Stop the game server to run this: the check has to listen on the game ports itself." : null}
              />
            </div>
          )}
            </div>
          </section>
        </div>
      )}
    </Card>
  );
}

function RemoteAccess({ auth }: { auth: AuthState | null }) {
  const { cfg, set } = useConfig();
  const { status } = useLive();
  const toast = useToast();
  const [changed, setChanged] = useState(false);
  if (!cfg || !auth) return null;
  const apply = async (value: boolean) => {
    set("network", "remote_access", value);
    try {
      await api("/config", { method: "PUT", body: { network: { remote_access: value } } });
      setChanged(true);
    } catch (e) {
      set("network", "remote_access", !value);
      toast.err(e);
    }
  };
  return (
    <Card title="Manage from other devices" desc="Lets browsers and the desktop app on other machines on your network reach this manager.">
      <div className="space-y-3">
        <Toggle
          label="Allow other devices to connect"
          hint={auth.needs_password ? "Set an admin password below first." : "Home and work networks only; never forward this port to the internet."}
          checked={cfg.network.remote_access}
          disabled={auth.needs_password && !cfg.network.remote_access}
          onChange={apply}
        />
        {changed && (
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-sky-500/30 bg-sky-500/10 px-4 py-2.5 text-sm text-sky-200">
            This takes effect when the manager restarts. The game server keeps running.
            {status?.service_mode ? (
              <Button
                onClick={() =>
                  api("/manager/restart", { method: "POST" })
                    .then(() => setChanged(false))
                    .catch(toast.err)
                }
              >
                Restart the manager now
              </Button>
            ) : (
              <span className="text-xs text-sky-300/80">Restart it yourself (close and reopen the app, or restart the container).</span>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

function Profiles() {
  const { data: profiles, setData } = useLoad<string[]>("/profiles");
  const [name, setName] = useState("");
  const locked = useLocked();
  const toast = useToast();
  const saveAs = (profile: string) =>
    api<string[]>("/profiles", { method: "POST", body: { name: profile } })
      .then((list) => {
        setData(list);
        setName("");
        toast.ok(`Profile “${profile}” saved.`);
      })
      .catch(toast.err);
  const path = (p: string) => `/profiles/${encodeURIComponent(p)}`;
  return (
    <Card title="Configuration profiles" desc="Snapshots of Game.ini, Engine.ini and launch settings, e.g. one for PvP weekends and one for PvE.">
      <form
        className="mb-4 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          saveAs(name.trim());
        }}
      >
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="New profile name" className="max-w-xs" aria-label="Profile name" />
        <Button type="submit" disabled={!name.trim()}>
          Save current settings
        </Button>
      </form>
      {!profiles?.length ? (
        <Empty>No profiles saved.</Empty>
      ) : (
        <ul className="divide-y divide-zinc-800">
          {profiles.map((p) => (
            <li key={p} className="flex items-center justify-between gap-3 py-2 text-sm first:pt-0 last:pb-0">
              <span className="text-zinc-200">{p}</span>
              <span className="flex gap-2">
                <Button
                  disabled={locked}
                  title={locked ? "Stop the server to load a profile" : ""}
                  onClick={() =>
                    confirm(`Load “${p}”? Current settings will be replaced.`) &&
                    api(`${path(p)}/load`, { method: "POST" })
                      .then(() => toast.ok(`Profile “${p}” loaded.`))
                      .catch(toast.err)
                  }
                >
                  Load
                </Button>
                <Button onClick={() => confirm(`Overwrite “${p}” with the current settings?`) && saveAs(p)}>Overwrite</Button>
                <Button variant="ghost" onClick={() => confirm(`Delete profile “${p}”?`) && api<string[]>(path(p), { method: "DELETE" }).then(setData).catch(toast.err)}>
                  Delete
                </Button>
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

interface AuthState {
  local: boolean;
  needs_password: boolean;
}

function Password({ auth, reload }: { auth: AuthState | null; reload: () => void }) {
  const { status } = useLive();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const toast = useToast();
  if (!auth) return null;
  // The desktop window is already trusted on this machine, so it never asks for the old password.
  // An installed Windows host was offered a password by Setup, so it is not nagged about it here.
  const firstTime = auth.local && auth.needs_password && !status?.service_mode;
  return (
    <Card
      title={firstTime ? "Set a password for remote access" : "Admin password"}
      desc={
        firstTime
          ? "This window signs in automatically. Other devices and browsers need a password, and none has been chosen yet."
          : "Changing it signs out every other session."
      }
      className={firstTime ? "border-sky-500/40" : undefined}
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          api("/auth/password", { method: "POST", body: { current, new: next } })
            .then(() => {
              toast.ok("Password saved.");
              setCurrent("");
              setNext("");
              reload();
            })
            .catch(toast.err);
        }}
      >
        <Grid>
          {!auth.local && (
            <Field label="Current password">
              <Input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
            </Field>
          )}
          <Field label="New password" hint="At least 8 characters.">
            <Input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
          </Field>
        </Grid>
        <Button type="submit" disabled={(!auth.local && !current) || next.length < 8}>
          {firstTime ? "Set password" : "Change password"}
        </Button>
      </form>
    </Card>
  );
}

export default function System() {
  const { events } = useLive();
  // One copy for the whole page: setting a password in one card must unlock "allow other devices" in another.
  const { data: auth, reload: reloadAuth } = useLoad<AuthState>("/auth/state");
  return (
    <>
      <PageHeader title="System" desc="Diagnostics, profiles and manager settings." />
      <div className="space-y-4">
        <Health />
        <RemoteAccess auth={auth} />
        <Profiles />
        <Password auth={auth} reload={reloadAuth} />
        <Card title="Event log" desc="Also written to logs/manager.log in the data directory. AUDIT lines (who signed in or changed what) are kept in logs/audit.log as well.">
          {events.length === 0 ? (
            <Empty>No events yet.</Empty>
          ) : (
            <div className="max-h-80 overflow-y-auto font-mono text-xs leading-5">
              {[...events].reverse().map((e, i) => (
                <div key={i} className="flex gap-3">
                  <span className="shrink-0 text-zinc-600">{new Date(e.time * 1000).toLocaleString()}</span>
                  <span className="w-24 shrink-0 text-zinc-400">{e.category}</span>
                  <span className="text-zinc-300">{e.message}</span>
                </div>
              ))}
            </div>
          )}
        </Card>
        <Card title="Guided setup" desc="The step-by-step setup shown when no game server is installed." actions={<Button onClick={() => (location.hash = "/setup")}>Run setup again</Button>}>
          <p className="text-sm text-zinc-400">Walks through the server name, ports, mods and start-up options, then installs or updates the game server.</p>
        </Card>
      </div>
    </>
  );
}
