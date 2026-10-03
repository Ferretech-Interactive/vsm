import { useEffect, useState } from "react";
import { fmtTime } from "../api";
import { useLive } from "../live";
import { Button, Card, Field, Grid, Input, PageHeader, Toggle, useConfig } from "../ui";
import { useUnsaved } from "../unsaved";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export default function Schedule() {
  const { cfg, set, save, dirty } = useConfig();
  const { status } = useLive();
  // Lists are edited as free text and parsed on save, so typing a comma is not fought by re-formatting.
  const [times, setTimes] = useState("");
  const [warnings, setWarnings] = useState("");
  const loaded = !!cfg;
  const [synced, setSynced] = useState(0); // bumped after a save, so the text shows what the server kept
  useEffect(() => {
    if (cfg) {
      setTimes(cfg.schedule.times.join(", "));
      setWarnings(cfg.schedule.warnings.join(", "));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded, synced]);
  const parse = (text: string) => text.split(/[\s,]+/).filter(Boolean);
  const commit = async () => {
    if (!cfg) return false;
    cfg.schedule.times = parse(times);
    cfg.schedule.warnings = parse(warnings).map(Number).filter((n) => Number.isFinite(n));
    const ok = await save("schedule", "behavior");
    if (ok) setSynced((n) => n + 1);
    return ok;
  };
  const textDirty = !!cfg && (times !== cfg.schedule.times.join(", ") || warnings !== cfg.schedule.warnings.join(", "));
  useUnsaved(dirty || textDirty, commit);
  if (!cfg) return null;

  const s = cfg.schedule;

  return (
    <>
      <PageHeader
        title="Schedule"
        desc={status?.next_restart ? `Next restart: ${fmtTime(status.next_restart)}` : "Automatic restarts and resource protection."}
        actions={
          <Button variant="primary" onClick={commit}>
            Save changes
          </Button>
        }
      />
      <div className="space-y-4">
        <Card title="Fixed-time restarts">
          <div className="space-y-4">
            <Toggle label="Restart at fixed times" hint="Uses the clock of the machine (or container) the manager runs on." checked={s.enabled} onChange={(v) => set("schedule", "enabled", v)} />
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Days">
              {DAYS.map((d, i) => (
                <button
                  key={d}
                  type="button"
                  aria-pressed={s.days[i]}
                  onClick={() => set("schedule", "days", s.days.map((on: boolean, j: number) => (j === i ? !on : on)))}
                  className={`rounded-lg border px-3 py-1.5 text-sm transition-colors ${s.days[i] ? "border-emerald-500 bg-emerald-600/20 text-emerald-300" : "border-zinc-700 text-zinc-500 hover:text-zinc-300"}`}
                >
                  {d}
                </button>
              ))}
            </div>
            <Grid>
              <Field label="Restart times" hint="24-hour HH:MM, separated by commas. Example: 04:00, 16:00">
                <Input value={times} onChange={(e) => setTimes(e.target.value)} />
              </Field>
              <Field label="Warnings (minutes before)" hint="Posted to the Discord webhook. Example: 30, 10, 5, 1">
                <Input value={warnings} onChange={(e) => setWarnings(e.target.value)} />
              </Field>
            </Grid>
          </div>
        </Card>

        <Card title="Uptime limit">
          <div className="space-y-4">
            <Toggle label="Restart after a maximum uptime" checked={s.uptime_enabled} onChange={(v) => set("schedule", "uptime_enabled", v)} />
            <Field label="Hours">
              <Input type="number" min={1} max={720} value={s.uptime_hours} onChange={(e) => set("schedule", "uptime_hours", Number(e.target.value))} disabled={!s.uptime_enabled} className="max-w-32" />
            </Field>
          </div>
        </Card>

        <Card title="Memory watchdog" desc="Restarts the server if a memory leak pushes it past the limit.">
          <Field label="Restart when RAM exceeds (GB)" hint="0 disables the watchdog.">
            <Input type="number" min={0} value={cfg.behavior.ram_limit_gb} onChange={(e) => set("behavior", "ram_limit_gb", Number(e.target.value))} className="max-w-32" />
          </Field>
        </Card>
      </div>
    </>
  );
}
