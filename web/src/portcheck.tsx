import { CircleAlert, CircleCheck, Loader2, Radar } from "lucide-react";
import { useState } from "react";
import { api } from "./api";
import { Button } from "./ui";

interface Result {
  public_ip: string;
  local_ip: string;
  behind_router: boolean;
  ports: { port: number; name: "game" | "query"; reachable: boolean }[];
  reachable: boolean;
  upnp: { ok: boolean; message: string; double_nat: boolean } | null;
  firewall_ready: boolean;
  firewall_managed: boolean;
}

function host(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function Advice({ result, windows }: { result: Result; windows: boolean }) {
  const closed = result.ports.filter((p) => !p.reachable).map((p) => p.port);
  if (result.upnp?.double_nat)
    return (
      <>
        Your router's own address is not a public one: there is a second router, or your internet provider shares one address between customers (CGNAT). Forwarding on your router cannot work
        by itself; ask the provider for a public IP address, or put the first router in bridge mode.
      </>
    );
  if (!result.firewall_ready) return <>Windows Firewall on this PC was not opened for the check in time. Try again in a moment; if it keeps happening, run Setup again to repair the firewall task.</>;
  if (!result.behind_router)
    return <>This machine has the public address itself, so no router is involved: a firewall (on this machine or at your hosting provider) is blocking UDP {closed.join(" and ")}.</>;
  return (
    <>
      On your router, forward UDP {closed.join(" and ")} to <span className="font-mono">{result.local_ip}</span>
      {result.upnp && !result.upnp.ok ? <> (the router refused to do it automatically: {result.upnp.message})</> : null}.
      {!result.firewall_managed && <> {windows ? "Also allow these ports in Windows Firewall on this PC." : "Also make sure this machine's firewall allows them."}</>} Then check again.
    </>
  );
}

/** An optional, on-request check of the game ports from the internet, with a plain statement of what it shares. */
export function PortCheck({
  port,
  queryPort,
  upnp,
  upnpAdapter,
  probeUrl,
  windows,
  disabled,
  inSetup = true,
}: {
  port: number;
  queryPort: number;
  upnp: boolean;
  /** The adapter chosen in the guided setup, which is not saved yet. Left out elsewhere: the saved one applies. */
  upnpAdapter?: string;
  probeUrl: string;
  windows: boolean;
  disabled?: string | null;
  /** False on the health check: the ports are the saved ones and nothing else depends on the result. */
  inSetup?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState("");
  if (!probeUrl) return null; // turned off in the settings
  const run = () => {
    setBusy(true);
    setError("");
    setResult(null);
    api<Result>("/portcheck", { method: "POST", body: { port, query_port: queryPort, upnp, upnp_adapter: upnpAdapter } })
      .then(setResult)
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-zinc-200">{inSetup ? "Check that players can reach these ports (optional)" : "Reachable from the internet"}</div>
          <p className="mt-1 text-xs leading-5 text-zinc-400">
            Tests your router's port forwarding from the internet. The manager listens on the two ports for a few seconds and asks a service run by FerreTech Interactive (
            <span className="font-mono text-zinc-300">{host(probeUrl)}</span>) to send a test packet to each.
          </p>
        </div>
        <Button onClick={run} disabled={busy || !!disabled} title={disabled ?? undefined} className="shrink-0 whitespace-nowrap">
          {busy ? <Loader2 size={15} className="animate-spin" /> : <Radar size={15} />}
          {busy ? "Checking…" : result ? "Check again" : "Check now"}
        </Button>
      </div>
      <div className="mt-3 flex gap-2 rounded-md border border-amber-500/25 bg-amber-500/5 px-3 py-2 text-xs leading-5 text-amber-100/90">
        <CircleAlert size={15} className="mt-0.5 shrink-0 text-amber-400" />
        <span>
          Pressing the button contacts that service over the internet. It sees <strong>this network's public IP address and the two port numbers</strong>, and sends packets only back to that
          address; it keeps no record.{" "}
          {inSetup
            ? `The port numbers above are saved first${upnp ? ", and your router is asked to forward them for the test" : ""}. Nothing is sent unless you press the button, and setup works the same without it.`
            : `${upnp ? "Your router is asked to forward the ports for the test. " : ""}Nothing is sent unless you press the button.`}
        </span>
      </div>
      {disabled && <p className="mt-2 text-xs text-zinc-500">{disabled}</p>}
      {error && <p className="mt-3 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-200">{error}</p>}
      {result && (
        <div className={`mt-3 rounded-md border px-3 py-2.5 text-sm ${result.reachable ? "border-emerald-500/30 bg-emerald-500/10" : "border-amber-500/30 bg-amber-500/10"}`}>
          <ul className="space-y-1">
            {result.ports.map((p) => (
              <li key={p.port} className="flex items-center gap-2">
                {p.reachable ? <CircleCheck size={16} className="text-emerald-400" /> : <CircleAlert size={16} className="text-amber-400" />}
                <span className="text-zinc-200">
                  {p.name === "game" ? "Game" : "Query"} port {p.port}/udp
                </span>
                <span className={p.reachable ? "text-emerald-300" : "text-amber-200"}>{p.reachable ? "reachable from the internet" : "not reachable"}</span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs leading-5 text-zinc-300">
            {result.reachable ? (
              <>
                Players can connect at <span className="font-mono">{result.public_ip}:{port}</span>.
                {upnp && result.upnp?.ok ? " The router forwarded the ports on request, and will do so whenever the server runs." : ""}
              </>
            ) : (
              <Advice result={result} windows={windows} />
            )}
          </p>
        </div>
      )}
    </div>
  );
}
