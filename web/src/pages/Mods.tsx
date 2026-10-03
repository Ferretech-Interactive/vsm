import { Plus, Trash2, Upload } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, fmtBytes } from "../api";
import { useLive } from "../live";
import { Badge, Button, Card, Empty, Input, LockNotice, PageHeader, Select, useLoad, useLocked, useToast } from "../ui";
import { useConfirmLeave, useUnsaved } from "../unsaved";
import Ue4ss, { Ue4ssUnsupported } from "./mods/Ue4ss";

interface Mod {
  name: string;
  enabled: boolean;
  size: number;
}

/** A folder of .pak mods: `/mods` is ~mods, `/logic-mods` is the blueprint mods UE4SS loads. */
function PakList({ endpoint, title, desc }: { endpoint: string; title: string; desc: string }) {
  const { data: mods, reload } = useLoad<Mod[]>(endpoint);
  const locked = useLocked();
  const toast = useToast();
  const picker = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);

  const upload = async (files: FileList | null) => {
    if (!files?.length) return;
    setUploading(true);
    try {
      for (const file of Array.from(files)) {
        const form = new FormData();
        form.append("file", file);
        await api(endpoint, { method: "POST", form });
      }
      toast.ok(`Uploaded ${files.length} file(s).`);
    } catch (e) {
      toast.err(e);
    } finally {
      setUploading(false);
      if (picker.current) picker.current.value = "";
      reload();
    }
  };
  const call = (path: string, method: string) => api(path, { method }).then(reload).catch(toast.err);

  return (
    <Card
      title={title}
      desc={desc}
      actions={
        <>
          <input ref={picker} type="file" accept=".pak,.utoc,.ucas" multiple hidden onChange={(e) => upload(e.target.files)} />
          <Button variant="primary" disabled={locked || uploading} onClick={() => picker.current?.click()}>
            <Upload size={15} /> {uploading ? "Uploading…" : "Upload .pak"}
          </Button>
        </>
      }
    >
      {!mods?.length ? (
        <Empty>No mods installed.</Empty>
      ) : (
        <ul className="divide-y divide-zinc-800">
          {mods.map((m) => (
            <li key={m.name} className="flex flex-wrap items-center justify-between gap-3 py-2.5 first:pt-0 last:pb-0">
              <div className="min-w-0">
                <div className="truncate text-sm text-zinc-200">{m.name}</div>
                <div className="text-xs text-zinc-500">{fmtBytes(m.size)}</div>
              </div>
              <div className="flex items-center gap-2">
                <Badge tone={m.enabled ? "green" : "zinc"}>{m.enabled ? "Active" : "Disabled"}</Badge>
                <Button disabled={locked} onClick={() => call(`${endpoint}/${encodeURIComponent(m.name)}/toggle`, "POST")}>
                  {m.enabled ? "Disable" : "Enable"}
                </Button>
                <Button variant="ghost" disabled={locked} aria-label={`Delete ${m.name}`} onClick={() => confirm(`Delete ${m.name}?`) && call(`${endpoint}/${encodeURIComponent(m.name)}`, "DELETE")}>
                  <Trash2 size={15} />
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function IniEditor() {
  const { data: sections, reload } = useLoad<string[]>("/ini/sections");
  const [section, setSection] = useState("");
  const [rows, setRows] = useState<[string, string][]>([]);
  const [savedRows, setSavedRows] = useState("[]"); // what the file holds, to tell whether `rows` was edited
  const locked = useLocked();
  const toast = useToast();
  const confirmLeave = useConfirmLeave();
  const entries = (list: [string, string][]) => list.filter(([k]) => k.trim());

  useEffect(() => {
    if (!section) {
      setSavedRows("[]");
      return setRows([]);
    }
    api<[string, string][]>(`/ini/section?name=${encodeURIComponent(section)}`)
      .then((loaded) => {
        setSavedRows(JSON.stringify(entries(loaded)));
        setRows(loaded);
      })
      .catch(toast.err);
  }, [section, toast]);

  const edit = (i: number, col: 0 | 1, value: string) => setRows(rows.map((r, j) => (j === i ? (col === 0 ? [value, r[1]] : [r[0], value]) : r)));
  const addSection = () => {
    const name = prompt("New section name, e.g. /Script/MyMod.Settings");
    if (name?.trim()) {
      setSection(name.trim());
      setRows([["", ""]]);
    }
  };
  const save = async () => {
    try {
      await api("/ini/section", { method: "PUT", body: { section, entries: entries(rows) } });
      setSavedRows(JSON.stringify(entries(rows)));
      toast.ok(`Saved [${section}].`);
      reload();
      return true;
    } catch (e) {
      toast.err(e);
      return false;
    }
  };
  useUnsaved(!!section && JSON.stringify(entries(rows)) !== savedRows, save);

  const options = sections && section && !sections.includes(section) ? [...sections, section] : (sections ?? []);

  return (
    <Card
      title="Mod config editor"
      desc="Edits mod sections of Game.ini. Core server sections are protected and managed on the Settings page."
      actions={
        <Button variant="primary" disabled={locked || !section} onClick={save}>
          Save section
        </Button>
      }
    >
      <div className="mb-4 flex gap-2">
        <Select
          value={section}
          onChange={(e) => {
            const next = e.target.value;
            confirmLeave(() => setSection(next));
          }}
          aria-label="Section"
        >
          <option value="">Select a section…</option>
          {options.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </Select>
        <Button onClick={addSection} disabled={locked} className="shrink-0">
          <Plus size={15} /> New section
        </Button>
      </div>
      {section && (
        <fieldset disabled={locked} className="space-y-2">
          {rows.map(([k, v], i) => (
            <div key={i} className="flex items-center gap-2">
              <Input value={k} onChange={(e) => edit(i, 0, e.target.value)} placeholder="Key (use +Key for array entries)" className="font-mono" aria-label="Key" />
              <span className="text-zinc-600">=</span>
              <Input value={v} onChange={(e) => edit(i, 1, e.target.value)} placeholder="Value" className="font-mono" aria-label="Value" />
              <Button variant="ghost" aria-label="Remove row" onClick={() => setRows(rows.filter((_, j) => j !== i))}>
                <Trash2 size={15} />
              </Button>
            </div>
          ))}
          <Button onClick={() => setRows([...rows, ["", ""]])}>
            <Plus size={15} /> Add row
          </Button>
        </fieldset>
      )}
    </Card>
  );
}

const TABS = [
  { id: "pak", label: "Pak mods" },
  { id: "ue4ss", label: "UE4SS" },
  { id: "blueprint", label: "Blueprint mods" },
  { id: "config", label: "Config editor" },
] as const;

export default function Mods() {
  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>("pak");
  const confirmLeave = useConfirmLeave();
  const { status } = useLive();
  const ue4ss = status?.capabilities.ue4ss ?? false;
  return (
    <>
      <PageHeader title="Mods" desc="Pak mods, UE4SS script mods and mod configuration." />
      <LockNotice />
      <div className="mb-4 flex gap-1 overflow-x-auto" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={t.id === tab}
            onClick={() => confirmLeave(() => setTab(t.id))}
            className={`shrink-0 rounded-lg px-3 py-1.5 text-sm transition-colors ${t.id === tab ? "bg-zinc-800 font-medium text-zinc-50" : "text-zinc-400 hover:text-zinc-200"}`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "pak" && <PakList endpoint="/mods" title="Pak mods" desc="Vein/Content/Paks/~mods. A mod can be a .pak alone or a .pak with matching .utoc and .ucas files." />}
      {tab === "ue4ss" && <Ue4ss />}
      {tab === "blueprint" &&
        (ue4ss ? (
          <PakList endpoint="/logic-mods" title="Blueprint mods" desc="Vein/Content/Paks/LogicMods. Loaded by UE4SS's BPModLoaderMod, so UE4SS must be installed and on." />
        ) : (
          <Ue4ssUnsupported />
        ))}
      {tab === "config" && <IniEditor />}
    </>
  );
}
