export interface Player {
  id: string;
  name: string;
}

/** One sample: the game server's CPU (% of the whole machine) and memory, and this machine's network traffic in bytes/s. */
export interface Usage {
  t: number;
  cpu: number;
  ram_mb: number;
  up: number;
  down: number;
}

export interface Status {
  state: "OFFLINE" | "STARTING" | "ONLINE" | "STOPPING" | "RESTARTING" | "UPDATING";
  task: string | null;
  pid: number | null;
  uptime: number;
  ram_mb: number;
  usage: Usage;
  /** Whether Steam has a different build of the configured branch than the one installed. */
  manager_update: { supported: boolean; state: "idle" | "working" | "restarting" | "failed"; step: string; error: string | null; rolled_back: string | null };
  game_update: { available: boolean; installed: string | null; latest: string | null; branch: string; checked_at: number };
  players: Player[];
  max_players: number;
  crash_count: number;
  save_active: boolean;
  installed: boolean;
  build_id: string | null;
  next_restart: number | null;
  platform: "windows" | "linux";
  capabilities: { ue4ss: boolean; upnp: boolean };
  service_mode: boolean;
  setup: {
    needed: boolean;
    running: boolean;
    steps: { name: string; state: "pending" | "running" | "done" | "failed" }[];
    error: string | null;
  };
  version: string;
}

export interface ManagerEvent {
  time: number;
  category: string;
  message: string;
}

// Mirrors vsm/config.py; sections are edited generically, so values stay loosely typed.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Config = Record<string, Record<string, any>>;

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

interface Options {
  method?: string;
  body?: unknown;
  form?: FormData;
}

export async function api<T = unknown>(path: string, { method = "GET", body, form }: Options = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: form ?? (body !== undefined ? JSON.stringify(body) : undefined),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* not JSON */
    }
    if (res.status === 401 && !path.startsWith("/auth/")) window.dispatchEvent(new Event("vsm:unauthorized"));
    throw new ApiError(detail, res.status);
  }
  return res.json();
}

export function fmtDuration(seconds: number): string {
  if (!seconds) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return m ? `${m}m` : `${seconds}s`;
}

export function fmtBytes(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

export function fmtTime(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
