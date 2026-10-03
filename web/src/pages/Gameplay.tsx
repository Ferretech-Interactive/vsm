import { RotateCcw } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { useUnsaved } from "../unsaved";
import { Button, Card, Input, LockNotice, PageHeader, Select, Toggle, useLoad, useLocked, useToast } from "../ui";

type Value = string | boolean;

interface Setting {
  label: string;
  key: string;
  help: string;
  type: "bool" | "number" | "scarcity";
  default: Value;
  options?: { value: string; label: string }[];
}

interface Gameplay {
  schema: { category: string; settings: Setting[] }[];
  values: Record<string, Value>;
}

export default function GameplayPage() {
  const { data, setData, setSaved, dirty } = useLoad<Gameplay>("/gameplay");
  const [tab, setTab] = useState(0);
  const locked = useLocked();
  const toast = useToast();
  const save = async () => {
    if (!data) return false;
    try {
      const values = await api<Record<string, Value>>("/gameplay", { method: "PUT", body: data.values });
      setSaved({ ...data, values });
      toast.ok("Gameplay settings saved.");
      return true;
    } catch (e) {
      toast.err(e);
      return false;
    }
  };
  useUnsaved(dirty, save);
  if (!data) return null;

  const set = (key: string, value: Value) => setData({ ...data, values: { ...data.values, [key]: value } });
  const reset = () => {
    if (!confirm("Reset every gameplay setting to the vanilla defaults? Nothing is written until you save.")) return;
    const values = Object.fromEntries(data.schema.flatMap((c) => c.settings.map((s) => [s.key, s.default])));
    setData({ ...data, values });
  };
  const category = data.schema[tab];

  return (
    <>
      <PageHeader
        title="Gameplay"
        desc="Loot, survival, zombies and PvP rules."
        actions={
          <>
            <Button onClick={reset} disabled={locked}>
              Reset to vanilla
            </Button>
            <Button variant="primary" onClick={save} disabled={locked}>
              Save changes
            </Button>
          </>
        }
      />
      <LockNotice />
      <div className="mb-4 flex gap-1 overflow-x-auto" role="tablist">
        {data.schema.map((c, i) => (
          <button
            key={c.category}
            role="tab"
            aria-selected={i === tab}
            onClick={() => setTab(i)}
            className={`shrink-0 rounded-lg px-3 py-1.5 text-sm transition-colors ${i === tab ? "bg-zinc-800 font-medium text-zinc-50" : "text-zinc-400 hover:text-zinc-200"}`}
          >
            {c.category}
          </button>
        ))}
      </div>
      <Card>
        <fieldset disabled={locked} className="divide-y divide-zinc-800">
          {category.settings.map((s) => {
            const value = data.values[s.key];
            const changed = value !== s.default;
            return (
              <div key={s.key} className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 py-3 first:pt-0 last:pb-0">
                <div className="min-w-0">
                  <div className="text-sm text-zinc-200">
                    {s.label}
                    {changed && (
                      <>
                        <span className="ml-2 text-xs text-emerald-400">modified</span>
                        <button
                          type="button"
                          onClick={() => set(s.key, s.default)}
                          title={`Reset to the game default (${String(s.default)})`}
                          aria-label={`Reset ${s.label} to the game default`}
                          className="ml-1.5 inline-flex rounded p-0.5 align-middle text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 disabled:pointer-events-none disabled:opacity-50"
                        >
                          <RotateCcw size={13} />
                        </button>
                      </>
                    )}
                  </div>
                  <div className="text-xs text-zinc-500">
                    {s.help} <span className="text-zinc-600">Default: {String(s.default)}</span>
                  </div>
                </div>
                {s.type === "bool" ? (
                  <Toggle label="" checked={!!value} onChange={(v) => set(s.key, v)} disabled={locked} />
                ) : s.type === "scarcity" ? (
                  <Select value={String(value)} onChange={(e) => set(s.key, e.target.value)} className="!w-44" aria-label={s.label}>
                    {s.options!.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </Select>
                ) : (
                  <Input value={String(value)} onChange={(e) => set(s.key, e.target.value)} inputMode="decimal" className="!w-28 text-right tabular-nums" aria-label={s.label} />
                )}
              </div>
            );
          })}
        </fieldset>
      </Card>
    </>
  );
}
