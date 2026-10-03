import {
  Archive,
  CalendarClock,
  Download,
  Info,
  LayoutDashboard,
  LogOut,
  Package,
  Play,
  Plug,
  RotateCw,
  Settings as SettingsIcon,
  SlidersHorizontal,
  Square,
  Users,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, type FormEvent, type JSX } from "react";
import { api, type Status, fmtDuration } from "./api";
import { IN_DESKTOP_APP } from "./desktop";
import { LiveProvider, useLive } from "./live";
import About from "./pages/About";
import Backups from "./pages/Backups";
import Dashboard from "./pages/Dashboard";
import Gameplay from "./pages/Gameplay";
import Install from "./pages/Install";
import Integrations from "./pages/Integrations";
import Mods from "./pages/Mods";
import Players from "./pages/Players";
import Schedule from "./pages/Schedule";
import Settings from "./pages/Settings";
import Setup, { setupSkipped } from "./pages/Setup";
import System from "./pages/System";
import { Button, Input, ToastProvider, useToast } from "./ui";
import { GameUpdateNotice, UpdateNotice } from "./update";
import vsmLogo from "./assets/vsm-logo.png";
import { hasUnsaved, UnsavedProvider, useConfirmLeave } from "./unsaved";

const PAGES: { id: string; label: string; icon: LucideIcon; component: () => JSX.Element | null }[] = [
  { id: "dashboard", label: "Dashboard", icon: LayoutDashboard, component: Dashboard },
  { id: "settings", label: "Server Settings", icon: SettingsIcon, component: Settings },
  { id: "gameplay", label: "Gameplay", icon: SlidersHorizontal, component: Gameplay },
  { id: "mods", label: "Mods", icon: Package, component: Mods },
  { id: "players", label: "Players", icon: Users, component: Players },
  { id: "schedule", label: "Schedule", icon: CalendarClock, component: Schedule },
  { id: "backups", label: "Backups", icon: Archive, component: Backups },
  { id: "install", label: "Install / Update", icon: Download, component: Install },
  { id: "integrations", label: "Integrations", icon: Plug, component: Integrations },
  { id: "system", label: "System", icon: Wrench, component: System },
  // The desktop app has Help > About in its menu bar; a browser has no menu bar, so it gets a page.
  ...(IN_DESKTOP_APP ? [] : [{ id: "about", label: "About", icon: Info, component: About }]),
];

const STATE_STYLE: Record<Status["state"], { dot: string; text: string }> = {
  ONLINE: { dot: "bg-emerald-400", text: "text-emerald-400" },
  OFFLINE: { dot: "bg-zinc-500", text: "text-zinc-400" },
  STARTING: { dot: "bg-amber-400 animate-pulse", text: "text-amber-400" },
  STOPPING: { dot: "bg-amber-400 animate-pulse", text: "text-amber-400" },
  RESTARTING: { dot: "bg-amber-400 animate-pulse", text: "text-amber-400" },
  UPDATING: { dot: "bg-sky-400 animate-pulse", text: "text-sky-400" },
};

function usePage(): [string, (id: string) => void] {
  const read = () => location.hash.replace(/^#\/?/, "") || "dashboard";
  const [page, setPage] = useState(read);
  const confirmLeave = useConfirmLeave();
  const shown = useRef(page);
  shown.current = page;
  useEffect(() => {
    const onHash = () => {
      const target = read();
      if (target === shown.current) return;
      if (!hasUnsaved()) return setPage(target);
      // Back button, a typed address or a link: put the address back, then ask.
      location.hash = `/${shown.current}`;
      confirmLeave(() => (location.hash = `/${target}`));
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [confirmLeave]);
  const go = useCallback((id: string) => confirmLeave(() => (location.hash = `/${id}`)), [confirmLeave]);
  return [page, go];
}

function Login({ onDone, installed }: { onDone: () => void; installed: boolean }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/auth/login", { method: "POST", body: { password } });
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <form onSubmit={submit} className="w-full max-w-sm rounded-2xl border border-zinc-800 bg-zinc-900/70 p-7">
        <Logo />
        <p className="mt-4 mb-5 text-sm text-zinc-400">
          {installed
            ? "Sign in with the admin password chosen during Setup. Administrators of this PC are signed in automatically when they open the app on it."
            : "Sign in with the admin password. On first run it is printed in the manager's console or container log."}
        </p>
        <Input type="password" autoFocus autoComplete="current-password" placeholder="Admin password" value={password} onChange={(e) => setPassword(e.target.value)} />
        {error && <p className="mt-2 text-sm text-red-400">{error}</p>}
        <Button type="submit" variant="primary" className="mt-4 w-full" disabled={busy || !password}>
          Sign in
        </Button>
      </form>
    </div>
  );
}

const Logo = () => (
  <img src={vsmLogo} alt="VSM — Vein Server Manager" width={600} height={260} className="mx-auto block h-auto w-full max-w-64" />
);

function ServerControls() {
  const { status } = useLive();
  const toast = useToast();
  if (!status) return null;
  const act = (action: string) => api(`/server/${action}`, { method: "POST" }).catch(toast.err);
  const busy = !!status.task;
  const style = STATE_STYLE[status.state];
  return (
    <div className="flex flex-wrap items-center gap-3">
      <div className="flex items-center gap-2 text-sm">
        <span className={`h-2.5 w-2.5 rounded-full ${style.dot}`} />
        <span className={`font-semibold ${style.text}`}>{status.state}</span>
        {/* Always shown: before the server is up this is 0 of the configured limit. */}
        <span className="text-zinc-500">
          {status.players.length}/{status.max_players} players
        </span>
        {status.pid && (
          <span className="text-zinc-500 tabular-nums" title="How long the game server has been running, and its process ID">
            · up {fmtDuration(status.uptime)} · PID {status.pid}
          </span>
        )}
      </div>
      <div className="flex gap-2">
        {status.state === "OFFLINE" ? (
          <Button variant="primary" disabled={busy || !status.installed} title={status.installed ? "" : "Install the server first"} onClick={() => act("start")}>
            <Play size={15} /> Start
          </Button>
        ) : (
          <>
            <Button disabled={busy} onClick={() => act("restart")}>
              <RotateCw size={15} /> Restart
            </Button>
            <Button variant="danger" disabled={busy} onClick={() => act("stop")}>
              <Square size={15} /> Stop
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

function Shell({ onLogout }: { onLogout: () => void }) {
  const [page, go] = usePage();
  const confirmLeave = useConfirmLeave();
  const { connected, status } = useLive();
  const current = PAGES.find((p) => p.id === page) ?? PAGES[0];
  const Page = current.component;
  // With no game server installed yet, open the guided setup once instead of an empty dashboard.
  const checkedSetup = useRef(false);
  useEffect(() => {
    if (status && !checkedSetup.current) {
      checkedSetup.current = true;
      if ((status.setup.needed && !setupSkipped()) || status.setup.running) go("setup");
    }
  }, [status, go]);
  if (page === "setup") return <Setup onDone={() => go("dashboard")} />;
  return (
    <div className="flex h-full flex-col md:flex-row">
      <aside className="flex shrink-0 flex-col border-b border-zinc-800 bg-zinc-900/40 md:w-56 md:border-r md:border-b-0">
        <div className="hidden px-4 py-4 md:block">
          <Logo />
        </div>
        <nav className="flex gap-1 overflow-x-auto p-2 md:flex-1 md:flex-col md:overflow-visible" aria-label="Main">
          {PAGES.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => go(id)}
              aria-current={id === current.id ? "page" : undefined}
              className={`flex shrink-0 items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors ${
                id === current.id ? "bg-zinc-800 font-medium text-zinc-50" : "text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-200"
              }`}
            >
              <Icon size={16} />
              {label}
            </button>
          ))}
        </nav>
        <div className="hidden items-center justify-between border-t border-zinc-800 px-4 py-3 text-xs text-zinc-500 md:flex">
          <span>v{status?.version ?? "…"}</span>
          <button onClick={() => confirmLeave(onLogout)} className="flex items-center gap-1 hover:text-zinc-200">
            <LogOut size={13} /> Sign out
          </button>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800 px-5 py-3">
          <ServerControls />
          <div className="flex items-center gap-3">
            {status?.game_update?.available && <GameUpdateNotice latest={status.game_update.latest} />}
            <UpdateNotice />
            {!connected && <span className="text-xs text-amber-400">Reconnecting…</span>}
          </div>
        </header>
        <main className="flex-1 overflow-y-auto p-5">
          <div className="mx-auto max-w-5xl">
            <Page />
          </div>
        </main>
      </div>
    </div>
  );
}

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [installed, setInstalled] = useState(false);
  useEffect(() => {
    api<{ authenticated: boolean; service_mode?: boolean }>("/auth/state")
      .then((s) => {
        setInstalled(!!s.service_mode);
        setAuthed(s.authenticated);
      })
      .catch(() => setAuthed(false));
    const onUnauthorized = () => setAuthed(false);
    window.addEventListener("vsm:unauthorized", onUnauthorized);
    return () => window.removeEventListener("vsm:unauthorized", onUnauthorized);
  }, []);
  if (authed === null) return null;
  if (!authed) return <Login onDone={() => setAuthed(true)} installed={installed} />;
  const logout = () => api("/auth/logout", { method: "POST" }).finally(() => setAuthed(false));
  return (
    <ToastProvider>
      <LiveProvider>
        <UnsavedProvider>
          <Shell onLogout={logout} />
        </UnsavedProvider>
      </LiveProvider>
    </ToastProvider>
  );
}
