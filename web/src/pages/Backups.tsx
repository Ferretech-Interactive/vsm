import { Download, Trash2 } from "lucide-react";
import { useState } from "react";
import { api, fmtBytes, fmtTime } from "../api";
import { Button, Card, Empty, Field, Grid, Input, PageHeader, Toggle, useConfig, useLoad, useLocked, useToast } from "../ui";
import { useUnsaved } from "../unsaved";

interface Backup {
  name: string;
  size: number;
  created: number;
}

export default function Backups() {
  const { cfg, set, save, dirty } = useConfig();
  useUnsaved(dirty, () => save("backups"));
  const { data: backups, reload } = useLoad<Backup[]>("/backups");
  const locked = useLocked();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  if (!cfg) return null;

  const run = (promise: Promise<unknown>, done: string) => {
    setBusy(true);
    promise
      .then(() => toast.ok(done))
      .catch(toast.err)
      .finally(() => {
        setBusy(false);
        reload();
      });
  };
  const path = (b: Backup) => `/backups/${encodeURIComponent(b.name)}`;

  return (
    <>
      <PageHeader
        title="Backups"
        desc="Zip archives of the server's Saved folder (worlds, characters and config; logs excluded)."
        actions={
          <Button variant="primary" disabled={busy} onClick={() => run(api("/backups", { method: "POST" }), "Backup created.")}>
            {busy ? "Working…" : "Create backup now"}
          </Button>
        }
      />
      <div className="space-y-4">
        <Card title="Automatic backups" actions={<Button onClick={() => save("backups")}>Save</Button>}>
          <div className="space-y-4">
            <Grid>
              <Field label="Every N hours while online" hint="0 disables interval backups.">
                <Input type="number" min={0} max={720} value={cfg.backups.interval_hours} onChange={(e) => set("backups", "interval_hours", Number(e.target.value))} />
              </Field>
              <Field label="Keep the newest" hint="Older archives are deleted. 0 keeps everything.">
                <Input type="number" min={0} max={1000} value={cfg.backups.retention} onChange={(e) => set("backups", "retention", Number(e.target.value))} />
              </Field>
            </Grid>
            <Field label="File name format" hint="strftime pattern, e.g. %Y-%m-%d_%H-%M-%S">
              <Input value={cfg.backups.name_format} onChange={(e) => set("backups", "name_format", e.target.value)} className="font-mono" />
            </Field>
            <Toggle label="Back up whenever the server stops" hint="Includes restarts. Taken after the process exits, so the save is consistent." checked={cfg.backups.on_stop} onChange={(v) => set("backups", "on_stop", v)} />
          </div>
        </Card>

        <Card title={`Archives (${backups?.length ?? 0})`}>
          {!backups?.length ? (
            <Empty>No backups yet.</Empty>
          ) : (
            <ul className="divide-y divide-zinc-800">
              {backups.map((b) => (
                <li key={b.name} className="flex flex-wrap items-center justify-between gap-3 py-2.5 first:pt-0 last:pb-0">
                  <div className="min-w-0">
                    <div className="truncate font-mono text-xs text-zinc-200">{b.name}</div>
                    <div className="text-xs text-zinc-500">
                      {fmtTime(b.created)} · {fmtBytes(b.size)}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <a href={`/api${path(b)}`} download className="inline-flex items-center gap-1.5 rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm font-medium text-zinc-100 hover:bg-zinc-700">
                      <Download size={15} /> Download
                    </a>
                    <Button
                      disabled={locked || busy}
                      title={locked ? "Stop the server to restore" : ""}
                      onClick={() => confirm(`Restore ${b.name}? Current save files will be overwritten.`) && run(api(`${path(b)}/restore`, { method: "POST" }), "Backup restored.")}
                    >
                      Restore
                    </Button>
                    <Button variant="ghost" aria-label={`Delete ${b.name}`} disabled={busy} onClick={() => confirm(`Delete ${b.name}?`) && run(api(path(b), { method: "DELETE" }), "Backup deleted.")}>
                      <Trash2 size={15} />
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}
