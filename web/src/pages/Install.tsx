import { Loader2, Lock, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, fmtTime } from "../api";
import { useLive } from "../live";
import { Badge, Button, Card, Console, Empty, Field, Input, PageHeader, Toggle, useConfig, useToast } from "../ui";
import { useUnsaved } from "../unsaved";

interface Branch {
  name: string;
  description: string;
  build_id: number;
  updated: number;
  password_required: boolean;
}

interface BranchList {
  loading: boolean;
  error: string;
  checked_at: number;
  branches: Branch[];
}

function ago(epochSeconds: number): string {
  const s = Math.max(0, Date.now() / 1000 - epochSeconds);
  const units: [number, string][] = [[31536000, "year"], [2592000, "month"], [86400, "day"], [3600, "hour"], [60, "minute"]];
  for (const [size, name] of units) {
    const n = Math.floor(s / size);
    if (n >= 1) return `${n} ${name}${n > 1 ? "s" : ""} ago`;
  }
  return "just now";
}

/** The stored branch list; the manager refreshes it in the background and this polls while it does. */
function useBranches() {
  const [list, setList] = useState<BranchList | null>(null);
  const toast = useToast();
  const load = useCallback(() => {
    api<BranchList>("/steam/branches").then(setList).catch(toast.err);
  }, [toast]);
  useEffect(load, [load]);
  useEffect(() => {
    if (!list?.loading) return;
    const timer = setTimeout(load, 1500);
    return () => clearTimeout(timer);
  }, [list, load]);
  const refresh = () => api<BranchList>("/steam/branches/refresh", { method: "POST" }).then(setList).catch(toast.err);
  return { list, refresh };
}

function Branches({ list, refresh, current, installedBuild, onPick }: { list: BranchList | null; refresh: () => void; current: string; installedBuild?: string | null; onPick: (name: string) => void }) {
  const loading = !list || list.loading;
  return (
    <Card
      title="Branches"
      desc="The official list from Steam. Pick one, then install or update."
      actions={
        <span className="flex items-center gap-3">
          <span className="text-xs text-zinc-500">
            {loading ? "Asking Steam…" : list.checked_at ? `Checked ${ago(list.checked_at)}` : "Not checked yet"}
          </span>
          <Button onClick={refresh} disabled={loading} title="Ask Steam for the current list">
            {loading ? <Loader2 size={15} className="animate-spin" /> : <RefreshCw size={15} />}
            Refresh
          </Button>
        </span>
      }
    >
      {list?.error && !loading && <p className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-200">{list.error}</p>}
      {!list?.branches.length ? (
        loading ? (
          <div className="flex items-center justify-center gap-2 py-8 text-sm text-zinc-400">
            <Loader2 size={18} className="animate-spin text-sky-400" />
            Loading the branch list from Steam. The first time can take a minute.
          </div>
        ) : (
          <Empty>No branch list yet. Press Refresh to ask Steam.</Empty>
        )
      ) : (
        <div className={`max-h-96 overflow-auto transition-opacity ${loading ? "opacity-60" : ""}`}>
          <table className="w-full text-left text-sm">
            <thead className="sticky top-0 bg-zinc-900 text-xs text-zinc-500">
              <tr>
                <th className="px-3 py-2 font-medium">Name</th>
                <th className="px-3 py-2 font-medium">Description</th>
                <th className="px-3 py-2 font-medium">Build ID</th>
                <th className="px-3 py-2 font-medium">Updated</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800">
              {list.branches.map((b) => {
                const selected = b.name === current;
                return (
                  <tr key={b.name} className={selected ? "bg-sky-500/10" : "hover:bg-zinc-800/40"}>
                    <td className={`px-3 py-2 font-mono ${selected ? "font-semibold text-sky-200" : "text-zinc-200"}`}>{b.name}</td>
                    <td className="px-3 py-2 text-zinc-400">{b.description}</td>
                    <td className="px-3 py-2 font-mono tabular-nums text-zinc-300">
                      {b.build_id || "—"}
                      {installedBuild && String(b.build_id) === installedBuild && <span className="ml-2 text-xs text-emerald-400">installed</span>}
                    </td>
                    <td className="px-3 py-2 text-zinc-400" title={b.updated ? fmtTime(b.updated) : undefined}>
                      {b.updated ? ago(b.updated) : "—"}
                    </td>
                    <td className="px-3 py-1.5 text-right">
                      {b.password_required ? (
                        <span className="inline-flex items-center gap-1 text-xs text-zinc-500" title="This branch needs a password from the developers">
                          <Lock size={13} /> Private
                        </span>
                      ) : selected ? (
                        <Badge tone="blue">Selected</Badge>
                      ) : (
                        <Button variant="ghost" onClick={() => onPick(b.name)}>
                          Select
                        </Button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

export default function Install() {
  const { status, steam } = useLive();
  const { cfg, set, save, dirty } = useConfig();
  useUnsaved(dirty, () => save("behavior"));
  const toast = useToast();
  const { list, refresh } = useBranches();
  if (!status || !cfg) return null;

  const idle = status.state === "OFFLINE" && !status.task;
  const run = async (validate: boolean) => {
    // Persist the branch first so SteamCMD pulls the one shown on screen.
    if (!(await save("behavior"))) return;
    api("/steam/update", { method: "POST", body: { validate_files: validate } }).catch(toast.err);
  };

  return (
    <>
      <PageHeader
        title="Install / Update"
        desc="Server files are downloaded with SteamCMD (anonymous login, app 2131400). SteamCMD itself is fetched automatically."
        actions={
          !status.installed ? (
            <Badge tone="amber">Not installed</Badge>
          ) : (
            <span className="flex flex-wrap items-center justify-end gap-2">
              {status.game_update?.available && <Badge tone="amber">Update available · build {status.game_update.latest}</Badge>}
              <Badge tone="green">Installed · build {status.build_id ?? "?"}</Badge>
            </span>
          )
        }
      />
      <div className="space-y-4">
        <Card title="Server files">
          <div className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Steam branch" hint="Pick one from the list below, or type its name.">
                <Input list="branches" value={cfg.behavior.steam_branch} onChange={(e) => set("behavior", "steam_branch", e.target.value)} />
                <datalist id="branches">
                  {(list?.branches.length ? list.branches.filter((b) => !b.password_required).map((b) => b.name) : ["public", "experimental"]).map((name) => (
                    <option key={name} value={name} />
                  ))}
                </datalist>
              </Field>
              <div className="pt-5">
                <Toggle label="Check for updates on every start" hint="Runs SteamCMD before launching. Adds a little time to each start." checked={cfg.behavior.update_on_start} onChange={(v) => set("behavior", "update_on_start", v)} />
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="primary" disabled={!idle} onClick={() => run(false)}>
                {status.installed ? "Update now" : "Install server"}
              </Button>
              <Button disabled={!idle || !status.installed} onClick={() => run(true)}>
                Validate files (slow)
              </Button>
              <Button variant="ghost" onClick={() => save("behavior")}>
                Save options only
              </Button>
              {!idle && <span className="text-xs text-zinc-500">{status.state === "UPDATING" ? "SteamCMD is running…" : "Stop the server to update."}</span>}
            </div>
          </div>
        </Card>
        <Branches list={list} refresh={refresh} current={cfg.behavior.steam_branch} installedBuild={status.build_id} onPick={(name) => set("behavior", "steam_branch", name)} />
        <Card title="SteamCMD output" desc="Also written to logs/steamcmd.log in the data directory.">
          <Console lines={steam} className="h-96" />
        </Card>
      </div>
    </>
  );
}
