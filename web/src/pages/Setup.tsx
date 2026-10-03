import { Check, Circle, Loader2, X } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { api, type Config } from "../api";
import { useLive } from "../live";
import { PortCheck } from "../portcheck";
import { UpnpRouter } from "../upnp";
import { SteamIdList } from "../steam";
import { Button, Console, Field, Grid, Input, Toggle, useToast } from "../ui";

export const SETUP_SKIPPED = "vsm.setupSkipped";

export function setupSkipped(): boolean {
  try {
    return sessionStorage.getItem(SETUP_SKIPPED) === "1";
  } catch {
    return false;
  }
}

interface Form {
  adminPassword: string;
  adminPassword2: string;
  serverName: string;
  description: string;
  joinPassword: string;
  maxPlayers: number;
  superAdmins: string[];
  port: number;
  queryPort: number;
  upnp: boolean;
  upnpAdapter: string;
  probeUrl: string;
  remote: boolean;
  ue4ss: boolean;
  autoStart: boolean;
  keepAlive: boolean;
  backupHours: number;
}

interface GameSettings {
  server_name: string;
  description: string;
  password: string;
  super_admins: string[];
}

interface AuthState {
  needs_password: boolean;
}

const validPort = (n: number) => Number.isInteger(n) && n >= 1024 && n <= 65535;

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-1.5 text-sm">
      <span className="text-zinc-400">{label}</span>
      <span className="text-right text-zinc-200">{children}</span>
    </div>
  );
}

/** First-time guided setup. The work itself runs on the server, so closing this page does not interrupt it. */
export default function Setup({ onDone }: { onDone: () => void }) {
  const { status, steam } = useLive();
  const toast = useToast();
  const [form, setForm] = useState<Form | null>(null);
  const [needsPassword, setNeedsPassword] = useState(false);
  const [step, setStep] = useState(0);
  const [submitted, setSubmitted] = useState(false);

  useEffect(() => {
    Promise.all([api<Config>("/config"), api<GameSettings>("/game-settings"), api<AuthState>("/auth/state")])
      .then(([cfg, game, auth]) => {
        setNeedsPassword(auth.needs_password);
        setForm({
          adminPassword: "",
          adminPassword2: "",
          serverName: game.server_name,
          description: game.description,
          joinPassword: game.password,
          maxPlayers: cfg.launch.max_players,
          superAdmins: game.super_admins,
          port: cfg.launch.port,
          queryPort: cfg.launch.query_port,
          upnp: cfg.network.upnp,
          upnpAdapter: cfg.network.upnp_adapter ?? "",
          probeUrl: cfg.network.probe_url ?? "",
          remote: cfg.network.remote_access,
          ue4ss: false,
          autoStart: true,
          keepAlive: cfg.behavior.keep_alive,
          backupHours: cfg.backups.interval_hours,
        });
      })
      .catch(toast.err);
  }, [toast]);

  if (!status || !form) return null;
  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm({ ...form, [key]: value });
  const windows = status.platform === "windows";
  // An installed Windows host was offered a password by Setup.exe; everywhere else ask here.
  const askPassword = needsPassword && !status.service_mode;
  const hasPassword = !needsPassword || form.adminPassword.length >= 8;
  const working = status.setup.running;
  const finished = submitted && !working && !status.setup.error && status.installed;
  const showProgress = working || finished || (submitted && !!status.setup.error);

  const steps: { title: string; valid: () => string | null; body: ReactNode }[] = [];

  if (askPassword) {
    steps.push({
      title: "Access",
      valid: () => {
        if (!form.adminPassword && !form.adminPassword2) return null;
        if (form.adminPassword.length < 8) return "The password must be at least 8 characters, or left empty.";
        return form.adminPassword === form.adminPassword2 ? null : "The two passwords are not the same.";
      },
      body: (
        <>
          <p className="text-sm text-zinc-400">
            Choose a password for managing this server from a web browser or another device. You can leave it empty and set one later under System.
          </p>
          <Grid>
            <Field label="Admin password" hint="At least 8 characters.">
              <Input type="password" autoComplete="new-password" value={form.adminPassword} onChange={(e) => set("adminPassword", e.target.value)} />
            </Field>
            <Field label="Type it again">
              <Input type="password" autoComplete="new-password" value={form.adminPassword2} onChange={(e) => set("adminPassword2", e.target.value)} />
            </Field>
          </Grid>
        </>
      ),
    });
  }

  steps.push({
    title: "Server",
    valid: () => {
      if (!form.serverName.trim()) return "Give the server a name.";
      if (!/^[A-Za-z0-9_-]*$/.test(form.joinPassword)) return "The join password may only contain letters, numbers, dashes and underscores.";
      if (!(form.maxPlayers >= 1 && form.maxPlayers <= 512)) return "Max players must be between 1 and 512.";
      return null;
    },
    body: (
      <>
        <Grid>
          <Field label="Server name" hint="Shown in the server browser.">
            <Input value={form.serverName} maxLength={128} onChange={(e) => set("serverName", e.target.value)} />
          </Field>
          <Field label="Max players">
            <Input type="number" min={1} max={512} value={form.maxPlayers} onChange={(e) => set("maxPlayers", Number(e.target.value))} />
          </Field>
        </Grid>
        <Field label="Description">
          <Input value={form.description} maxLength={512} onChange={(e) => set("description", e.target.value)} />
        </Field>
        <Grid>
          <Field label="Join password" hint="Leave empty for a public server.">
            <Input value={form.joinPassword} autoComplete="off" onChange={(e) => set("joinPassword", e.target.value)} />
          </Field>
          <Field label="Super admins" hint="Usually you: full control in game. Optional.">
            <SteamIdList ids={form.superAdmins} onChange={(ids) => set("superAdmins", ids)} addLabel="Add super admin" pickerTitle="Add a super admin" empty="None yet." />
          </Field>
        </Grid>
      </>
    ),
  });

  steps.push({
    title: "Network",
    valid: () => {
      if (!validPort(form.port) || !validPort(form.queryPort)) return "Ports must be whole numbers from 1024 to 65535.";
      if (form.port === form.queryPort) return "The game port and query port must be different.";
      return form.remote && !hasPassword ? "Managing the server from other devices needs an admin password." : null;
    },
    body: (
      <>
        <Grid>
          <Field label="Game port (UDP)">
            <Input type="number" min={1024} max={65535} value={form.port} onChange={(e) => set("port", Number(e.target.value))} />
          </Field>
          <Field label="Query port (UDP)">
            <Input type="number" min={1024} max={65535} value={form.queryPort} onChange={(e) => set("queryPort", Number(e.target.value))} />
          </Field>
        </Grid>
        <p className="text-xs text-zinc-500">
          {status.service_mode
            ? "Windows Firewall on this PC is opened for these two ports automatically, for the game server only."
            : windows
              ? "Allow these two UDP ports in Windows Firewall yourself; this copy of the manager cannot change firewall rules."
              : "Make sure these two UDP ports are allowed by this machine's firewall and published if you use Docker."}{" "}
          Players outside your network also need the ports forwarded on your router.
        </p>
        {status.capabilities.upnp && (
          <Toggle
            label="Ask my router to forward these ports (UPnP)"
            hint="Saves setting up port forwarding by hand. Only the two game ports are forwarded, and only while the server runs."
            checked={form.upnp}
            onChange={(v) => set("upnp", v)}
          />
        )}
        {status.capabilities.upnp && form.upnp && <UpnpRouter value={form.upnpAdapter} onChange={(v) => set("upnpAdapter", v)} />}
        <Toggle
          label="Let other devices manage this server"
          hint={hasPassword ? "Opens the manager to your home or work network. Takes effect when the manager restarts." : askPassword ? "Needs an admin password first (previous step)." : "Needs an admin password first. Set one under System after setup, then turn this on there."}
          checked={form.remote}
          disabled={!hasPassword}
          onChange={(v) => set("remote", v)}
        />
        <PortCheck
          port={form.port}
          queryPort={form.queryPort}
          upnp={form.upnp && status.capabilities.upnp}
          upnpAdapter={form.upnpAdapter}
          probeUrl={form.probeUrl}
          windows={windows}
          disabled={
            !validPort(form.port) || !validPort(form.queryPort) || form.port === form.queryPort
              ? "Enter two different valid ports first."
              : status.state !== "OFFLINE" || status.task
                ? "Available while the game server is stopped: the check has to listen on these ports itself."
                : null
          }
        />
      </>
    ),
  });

  if (status.capabilities.ue4ss) {
    steps.push({
      title: "Mods",
      valid: () => null,
      body: (
        <>
          <Toggle
            label="Install UE4SS for script mods"
            hint="Needed for Lua, C++ and blueprint mods. You can also add it later on the Mods page."
            checked={form.ue4ss}
            onChange={(v) => set("ue4ss", v)}
          />
          <p className="text-xs text-zinc-500">Pak mods do not need UE4SS and can be uploaded on the Mods page at any time.</p>
        </>
      ),
    });
  }

  steps.push({
    title: "Running",
    valid: () => (form.backupHours >= 0 && form.backupHours <= 720 ? null : "Backup interval must be between 0 and 720 hours."),
    body: (
      <>
        <Toggle label="Start the game server whenever the manager starts" hint="Brings the server back after a reboot." checked={form.autoStart} onChange={(v) => set("autoStart", v)} />
        <Toggle label="Restart the server if it crashes" checked={form.keepAlive} onChange={(v) => set("keepAlive", v)} />
        <Field label="Back up the world every N hours" hint="0 turns scheduled backups off.">
          <Input type="number" min={0} max={720} value={form.backupHours} onChange={(e) => set("backupHours", Number(e.target.value))} className="max-w-32" />
        </Field>
      </>
    ),
  });

  steps.push({
    title: "Review",
    valid: () => null,
    body: (
      <div className="divide-y divide-zinc-800">
        <Row label="Server name">{form.serverName}</Row>
        <Row label="Players">up to {form.maxPlayers}</Row>
        <Row label="Join password">{form.joinPassword ? "set" : "none (public)"}</Row>
        <Row label="Ports">
          {form.port} and {form.queryPort} (UDP)
        </Row>
        {status.capabilities.upnp && <Row label="Router port forwarding (UPnP)">{form.upnp ? (form.upnpAdapter ? `yes, on ${form.upnpAdapter}` : "yes") : "no"}</Row>}
        <Row label="Manage from other devices">{form.remote ? "yes" : "no"}</Row>
        {status.capabilities.ue4ss && <Row label="UE4SS">{form.ue4ss ? "install" : "not now"}</Row>}
        <Row label="Start with the manager">{form.autoStart ? "yes" : "no"}</Row>
        <Row label="Backups">{form.backupHours ? `every ${form.backupHours} h` : "manual only"}</Row>
        <p className="pt-3 text-xs text-zinc-500">Install downloads the Vein dedicated server (several GB) and then starts it.</p>
      </div>
    ),
  });

  const current = steps[Math.min(step, steps.length - 1)];
  const last = step >= steps.length - 1;

  const install = () => {
    api("/setup", {
      method: "POST",
      body: {
        admin_password: askPassword ? form.adminPassword : "",
        config: {
          launch: { port: form.port, query_port: form.queryPort, max_players: form.maxPlayers },
          network: { upnp: form.upnp, upnp_adapter: form.upnpAdapter, remote_access: form.remote },
          behavior: { auto_start: form.autoStart, keep_alive: form.keepAlive },
          backups: { interval_hours: form.backupHours },
        },
        game: { server_name: form.serverName, description: form.description, password: form.joinPassword, super_admins: form.superAdmins },
        ue4ss: form.ue4ss,
        start: true,
      },
    })
      .then(() => setSubmitted(true))
      .catch(toast.err);
  };
  const next = () => {
    const problem = current.valid();
    if (problem) return toast.err(problem);
    if (last) install();
    else setStep(step + 1);
  };
  // "For now" means this window only: the setup comes back the next time the app is opened without a server.
  const skip = () => {
    try {
      sessionStorage.setItem(SETUP_SKIPPED, "1");
    } catch {
      /* storage unavailable: it simply opens again on the next load */
    }
    onDone();
  };

  return (
    <div className="flex min-h-full items-start justify-center overflow-y-auto p-4 sm:p-8">
      <div className="w-full max-w-2xl">
        <h1 className="text-2xl font-semibold text-zinc-50">Set up your Vein server</h1>
        <p className="mt-1 mb-6 text-sm text-zinc-400">No game server is installed here yet. A few questions, then the manager downloads and starts it for you.</p>

        {showProgress ? (
          <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
            <ul className="space-y-2.5">
              {status.setup.steps.map((s) => (
                <li key={s.name} className="flex items-center gap-3 text-sm">
                  {s.state === "done" && <Check size={16} className="text-emerald-400" />}
                  {s.state === "running" && <Loader2 size={16} className="animate-spin text-sky-400" />}
                  {s.state === "failed" && <X size={16} className="text-red-400" />}
                  {s.state === "pending" && <Circle size={16} className="text-zinc-600" />}
                  <span className={s.state === "pending" ? "text-zinc-500" : "text-zinc-200"}>{s.name}</span>
                </li>
              ))}
            </ul>
            {status.setup.error && <p className="mt-4 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm text-red-300">{status.setup.error}</p>}
            {steam.length > 0 && <Console lines={steam} className="mt-4 h-56" />}
            <div className="mt-5 flex flex-wrap items-center gap-2">
              {finished && (
                <Button variant="primary" onClick={onDone}>
                  Go to the dashboard
                </Button>
              )}
              {!working && status.setup.error && (
                <>
                  <Button variant="primary" onClick={install}>
                    Try again
                  </Button>
                  <Button onClick={() => setSubmitted(false)}>Change settings</Button>
                </>
              )}
              {working && <span className="text-xs text-zinc-500">You can close this window; the installation carries on.</span>}
            </div>
          </div>
        ) : (
          <>
            <ol className="mb-4 flex flex-wrap gap-x-4 gap-y-1 text-xs" aria-label="Setup steps">
              {steps.map((s, i) => (
                <li key={s.title} className={i === step ? "font-medium text-emerald-400" : i < step ? "text-zinc-300" : "text-zinc-600"} aria-current={i === step ? "step" : undefined}>
                  {i + 1}. {s.title}
                </li>
              ))}
            </ol>
            <div className="space-y-4 rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">{current.body}</div>
            <div className="mt-5 flex flex-wrap items-center justify-between gap-2">
              <Button variant="ghost" onClick={skip}>
                Skip for now
              </Button>
              <div className="flex gap-2">
                {step > 0 && <Button onClick={() => setStep(step - 1)}>Back</Button>}
                <Button variant="primary" onClick={next}>
                  {last ? "Install" : "Next"}
                </Button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
