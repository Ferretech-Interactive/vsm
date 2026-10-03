import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api, fmtDuration, fmtTime, type Usage } from "../api";
import { useLive } from "../live";
import { Badge, Button, Card, Console, Empty, Input, PageHeader } from "../ui";

function Stat({ label, value, sub, chart, className = "" }: { label: string; value: ReactNode; sub?: ReactNode; chart?: ReactNode; className?: string }) {
  return (
    <div className={`rounded-xl border border-zinc-800 bg-zinc-900/60 px-4 py-3 ${className}`}>
      <div className="text-xs text-zinc-500">{label}</div>
      <div className="mt-1 text-lg font-semibold text-zinc-50 tabular-nums">{value}</div>
      {sub && <div className="text-xs text-zinc-500">{sub}</div>}
      {chart}
    </div>
  );
}

const HISTORY = 150; // matches the manager: five minutes at one sample every 2 s

/** The manager's recent samples, then one more with every status update. */
function useUsage(latest?: Usage): Usage[] {
  const [samples, setSamples] = useState<Usage[]>([]);
  useEffect(() => {
    api<Usage[]>("/usage")
      .then(setSamples)
      .catch(() => undefined);
  }, []);
  useEffect(() => {
    if (!latest?.t) return;
    setSamples((old) => (old.length && old[old.length - 1].t >= latest.t ? old : [...old, latest].slice(-HISTORY)));
  }, [latest]);
  return samples;
}

/** A small line graph of the last five minutes. `max` fixes the scale; otherwise it fits the data. */
function Spark({ series, max, label }: { series: { values: number[]; color: string }[]; max?: number; label: string }) {
  const width = 200;
  const height = 36;
  const top = Math.max(max ?? 0, ...series.flatMap((s) => s.values), 1e-9);
  const path = (values: number[]) =>
    values
      .map((v, i) => {
        const x = values.length > 1 ? ((HISTORY - values.length + i) / (HISTORY - 1)) * width : width;
        return `${i ? "L" : "M"}${x.toFixed(1)},${(height - 1 - (v / top) * (height - 2)).toFixed(1)}`;
      })
      .join(" ");
  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="mt-2 h-9 w-full" role="img" aria-label={label}>
      <line x1="0" y1={height - 0.5} x2={width} y2={height - 0.5} className="stroke-zinc-800" strokeWidth="1" />
      {series.map((s) => s.values.length > 1 && <path key={s.color} d={path(s.values)} fill="none" className={s.color} strokeWidth="1.5" vectorEffect="non-scaling-stroke" strokeLinejoin="round" />)}
    </svg>
  );
}

function fmtRate(bytesPerSecond: number): string {
  const bits = bytesPerSecond * 8;
  if (bits >= 1e6) return `${(bits / 1e6).toFixed(bits >= 1e7 ? 0 : 1)} Mbps`;
  if (bits >= 1e3) return `${Math.round(bits / 1e3)} kbps`;
  return `${Math.round(bits)} bps`;
}

interface Network {
  internal_ip: string;
  external_ip: string;
  game_port: number;
}

function Address({ label, ip, port, sub }: { label: string; ip?: string; port?: number; sub: string }) {
  const known = !!ip && ip !== "unknown";
  const address = known ? `${ip}:${port}` : "";
  const [copied, setCopied] = useState(false);
  return (
    <Stat
      label={label}
      value={
        known ? (
          <button
            type="button"
            title="Copy address"
            className="font-mono text-base hover:text-sky-300"
            onClick={() =>
              navigator.clipboard?.writeText(address).then(() => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              })
            }
          >
            {address}
          </button>
        ) : ip ? (
          "Unknown"
        ) : (
          "…"
        )
      }
      sub={copied ? "Copied" : sub}
    />
  );
}

export default function Dashboard() {
  const [net, setNet] = useState<Network | null>(null);
  useEffect(() => {
    api<Network>("/network")
      .then(setNet)
      .catch(() => setNet({ internal_ip: "unknown", external_ip: "unknown", game_port: 0 }));
  }, []);
  const { status, log, events } = useLive();
  const usage = useUsage(status?.usage);
  const [filter, setFilter] = useState("");
  const lines = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return f ? log.filter((l) => l.toLowerCase().includes(f)) : log;
  }, [log, filter]);

  if (!status) return null;
  const recent = events.slice(-8).reverse();

  return (
    <>
      <PageHeader title="Dashboard" desc={status.task ? `${status.task}…` : undefined} />

      {!status.installed && status.state !== "UPDATING" && (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-sky-500/30 bg-sky-500/10 px-5 py-4">
          <div>
            <div className="text-sm font-medium text-sky-200">The Vein dedicated server is not installed yet.</div>
            <div className="text-xs text-sky-300/70">The manager downloads SteamCMD and the server files for you.</div>
          </div>
          <Button variant="primary" onClick={() => (location.hash = "/install")}>
            Go to Install
          </Button>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 xl:grid-cols-3">
        <Stat
          label="CPU"
          value={status.pid ? `${status.usage.cpu.toFixed(1)}%` : "—"}
          sub="Game server, of this machine"
          chart={<Spark label="CPU over the last five minutes" max={100} series={[{ values: usage.map((u) => u.cpu), color: "stroke-emerald-400" }]} />}
        />
        <Stat
          label="Memory"
          value={status.ram_mb ? `${(status.ram_mb / 1024).toFixed(2)} GB` : "—"}
          sub="Game server"
          chart={<Spark label="Memory over the last five minutes" series={[{ values: usage.map((u) => u.ram_mb), color: "stroke-sky-400" }]} />}
        />
        <Stat
          label="Network"
          value={
            <span className="text-base">
              <span className="text-sky-300">↓ {fmtRate(status.usage.down)}</span> <span className="text-amber-300">↑ {fmtRate(status.usage.up)}</span>
            </span>
          }
          sub="All traffic on this machine"
          chart={
            <Spark
              label="Network traffic over the last five minutes"
              series={[
                { values: usage.map((u) => u.down), color: "stroke-sky-400" },
                { values: usage.map((u) => u.up), color: "stroke-amber-400" },
              ]}
            />
          }
        />
        <Stat
          label="Next restart"
          value={status.next_restart ? fmtDuration(Math.max(60, Math.round(status.next_restart - Date.now() / 1000))) : "—"}
          sub={status.next_restart ? fmtTime(status.next_restart) : "Nothing scheduled"}
        />
        <Address label="Internal IP" ip={net?.internal_ip} port={net?.game_port} sub="For players on your network" />
        <Address label="External IP" ip={net?.external_ip} port={net?.game_port} sub="For players over the internet" />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <Card title="Players online" className="lg:col-span-1">
          {status.players.length === 0 ? (
            <Empty>Nobody is online.</Empty>
          ) : (
            <ul className="space-y-1.5 text-sm">
              {status.players.map((p) => (
                <li key={p.id} className="flex items-center justify-between gap-2">
                  <span className="truncate text-zinc-200">{p.name}</span>
                  <span className="font-mono text-xs text-zinc-500">{p.id}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card
          title="Activity"
          className="lg:col-span-2"
          actions={
            <>
              {status.save_active && <Badge tone="amber">Saving…</Badge>}
              <Badge tone={status.crash_count ? "red" : "zinc"}>Crashes: {status.crash_count}</Badge>
              <Badge>Build {status.build_id ?? "—"}</Badge>
            </>
          }
        >
          {recent.length === 0 ? (
            <Empty>No activity yet.</Empty>
          ) : (
            <ul className="space-y-1 text-sm">
              {recent.map((e, i) => (
                <li key={i} className="flex gap-3">
                  <span className="w-16 shrink-0 text-xs leading-5 text-zinc-500 tabular-nums">{new Date(e.time * 1000).toLocaleTimeString()}</span>
                  <span className="w-24 shrink-0 text-xs leading-5 font-medium text-zinc-400">{e.category}</span>
                  <span className="text-zinc-300">{e.message}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card
        title="Server console"
        desc="Live output from Vein.log"
        className="mt-4"
        actions={<Input placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} className="w-48" aria-label="Filter log" />}
      >
        <Console lines={lines} className="h-96" />
      </Card>
    </>
  );
}
