import { ArrowDown, ArrowUp, RefreshCw, ShieldAlert, Trash2, Upload } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";
import { api, fmtBytes } from "../../api";
import { Badge, Button, Card, Console, Empty, Toggle, useLoad, useLocked, useToast } from "../../ui";

interface ScriptMod {
  name: string;
  enabled: boolean;
  kind: "lua" | "cpp" | "builtin" | "unknown";
  builtin: boolean;
  size: number;
}

interface Ue4ssStatus {
  supported: boolean;
  installed: boolean;
  enabled: boolean;
  version: string | null;
  source: "github" | "upload" | null;
  game_build: string | null;
  build_changed: boolean;
  game_config: boolean;
  mods: ScriptMod[];
}

/** Shown in place of the UE4SS and Blueprint tabs when the manager runs the Linux server build. */
export function Ue4ssUnsupported() {
  return (
    <Card title="UE4SS needs the Windows server build">
      <p className="text-sm text-zinc-400">
        This manager is running the Linux server, which UE4SS can't load into, so script mods and blueprint mods are unavailable here. Pak mods still work. To use UE4SS, run
        Vein Server Manager on Windows.
      </p>
    </Card>
  );
}

function Notice({ tone, children }: { tone: "amber" | "sky"; children: ReactNode }) {
  const tones = { amber: "border-amber-500/30 bg-amber-500/10 text-amber-300", sky: "border-sky-500/30 bg-sky-500/10 text-sky-200" };
  return <div className={`flex items-start gap-2 rounded-lg border px-4 py-2.5 text-sm ${tones[tone]}`}>{children}</div>;
}

function FilePicker({ accept, multiple, onFiles, children, disabled }: { accept: string; multiple?: boolean; onFiles: (files: File[]) => void; children: ReactNode; disabled?: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <input
        ref={input}
        type="file"
        accept={accept}
        multiple={multiple}
        hidden
        onChange={(e) => {
          onFiles(Array.from(e.target.files ?? []));
          e.target.value = "";
        }}
      />
      <Button disabled={disabled} onClick={() => input.current?.click()}>
        {children}
      </Button>
    </>
  );
}

export default function Ue4ss() {
  const { data: status, setData: setStatus, reload } = useLoad<Ue4ssStatus>("/ue4ss");
  const { data: log, reload: reloadLog } = useLoad<string[]>("/ue4ss/log");
  const [busy, setBusy] = useState("");
  const [showBuiltin, setShowBuiltin] = useState(false);
  const locked = useLocked();
  const toast = useToast();
  if (!status) return null;
  if (!status.supported) return <Ue4ssUnsupported />;

  const run = (label: string, work: () => Promise<unknown>, done?: string) => {
    setBusy(label);
    work()
      .then(() => done && toast.ok(done))
      .catch(toast.err)
      .finally(() => {
        setBusy("");
        reload();
      });
  };
  const uploadFile = (path: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api(path, { method: "POST", form });
  };
  const saveMods = (mods: ScriptMod[]) => {
    setStatus({ ...status, mods });
    api<ScriptMod[]>("/ue4ss/mods", { method: "PUT", body: mods.map(({ name, enabled }) => ({ name, enabled })) })
      .then((saved) => setStatus({ ...status, mods: saved }))
      .catch((e) => {
        toast.err(e);
        reload();
      });
  };
  const setEnabled = (name: string, enabled: boolean) => saveMods(status.mods.map((m) => (m.name === name ? { ...m, enabled } : m)));
  // Swap a mod with its nearest neighbour of the same group, leaving the other group's positions alone.
  const move = (name: string, direction: -1 | 1) => {
    const mods = [...status.mods];
    const from = mods.findIndex((m) => m.name === name);
    let to = from + direction;
    while (mods[to] && mods[to].builtin) to += direction;
    if (!mods[to]) return;
    [mods[from], mods[to]] = [mods[to], mods[from]];
    saveMods(mods);
  };

  const userMods = status.mods.filter((m) => !m.builtin);
  const builtinMods = status.mods.filter((m) => m.builtin);
  const disabled = locked || !!busy;

  return (
    <div className="space-y-4">
      <Card
        title="UE4SS framework"
        desc="Loads Lua, C++ and blueprint mods into the server."
        actions={
          status.installed ? (
            <Badge tone={status.enabled ? "green" : "amber"}>{status.enabled ? "Installed" : "Installed · off"}</Badge>
          ) : (
            <Badge>Not installed</Badge>
          )
        }
      >
        <div className="space-y-4">
          {status.installed && (
            <p className="text-sm text-zinc-400">
              Version <span className="text-zinc-200">{status.version ?? "unknown"}</span>
              {status.source === "upload" ? " (uploaded zip)" : " (official experimental build)"}
              {status.game_build && <> · installed for game build {status.game_build}</>}
            </p>
          )}
          {status.installed && !status.game_config && (
            <Notice tone="amber">
              <ShieldAlert size={16} className="mt-0.5 shrink-0" />
              UE4SS's settings for Vein are missing, and without them the server crashes as it finishes loading. Click "Update to latest" to fetch them.
            </Notice>
          )}
          {status.build_changed && (
            <Notice tone="amber">
              <ShieldAlert size={16} className="mt-0.5 shrink-0" />
              The game has been updated since UE4SS was installed. If the server crashes on start, update UE4SS or turn it off below.
            </Notice>
          )}
          {status.installed && (
            <Toggle
              label="Load UE4SS when the server starts"
              hint="Turn off to start the server without any script or blueprint mods (safe mode)."
              checked={status.enabled}
              disabled={disabled}
              onChange={(enabled) => run("toggle", () => api("/ue4ss/enabled", { method: "POST", body: { enabled } }))}
            />
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button variant={status.installed ? "default" : "primary"} disabled={disabled} onClick={() => run("install", () => api("/ue4ss/install", { method: "POST" }), "UE4SS installed.")}>
              <RefreshCw size={15} className={busy === "install" ? "animate-spin" : ""} />
              {busy === "install" ? "Downloading…" : status.installed ? "Update to latest" : "Install UE4SS"}
            </Button>
            <FilePicker accept=".zip" disabled={disabled} onFiles={([file]) => file && run("upload", () => uploadFile("/ue4ss/upload", file), "UE4SS installed from zip.")}>
              <Upload size={15} /> Install from zip
            </FilePicker>
            {status.installed && (
              <Button variant="ghost" disabled={disabled} onClick={() => confirm("Remove UE4SS? Your script mods are kept but will not load.") && run("remove", () => api("/ue4ss", { method: "DELETE" }), "UE4SS removed.")}>
                Remove
              </Button>
            )}
          </div>
          <p className="text-xs text-zinc-500">
            Install downloads the official experimental build and its Vein-specific settings from GitHub. Use "Install from zip" for a community package such as "UE4SS Prepackaged
            for VEIN".
          </p>
        </div>
      </Card>

      {status.installed && (
        <Card
          title="Script mods"
          desc="Loaded top to bottom."
          actions={
            <FilePicker
              accept=".zip"
              multiple
              disabled={disabled}
              onFiles={(files) =>
                files.length &&
                run(
                  "mod",
                  async () => {
                    for (const file of files) await uploadFile("/ue4ss/mods", file);
                  },
                  `Installed ${files.length} mod archive(s).`,
                )
              }
            >
              <Upload size={15} /> Upload mod zip
            </FilePicker>
          }
        >
          <Notice tone="sky">
            <ShieldAlert size={16} className="mt-0.5 shrink-0" />
            Script and C++ mods run code on this machine with the server's permissions. Only install mods you trust. Many mods must also be installed by every player.
          </Notice>
          {userMods.length === 0 ? (
            <Empty>No script mods installed. Upload a mod's .zip to add one.</Empty>
          ) : (
            <ul className="mt-3 divide-y divide-zinc-800">
              {userMods.map((m, i) => (
                <li key={m.name} className="flex flex-wrap items-center justify-between gap-3 py-2.5">
                  <div className="flex min-w-0 items-center gap-3">
                    <div className="flex flex-col">
                      <button aria-label={`Move ${m.name} up`} disabled={disabled || i === 0} onClick={() => move(m.name, -1)} className="text-zinc-500 hover:text-zinc-200 disabled:opacity-30">
                        <ArrowUp size={14} />
                      </button>
                      <button aria-label={`Move ${m.name} down`} disabled={disabled || i === userMods.length - 1} onClick={() => move(m.name, 1)} className="text-zinc-500 hover:text-zinc-200 disabled:opacity-30">
                        <ArrowDown size={14} />
                      </button>
                    </div>
                    <div className="min-w-0">
                      <div className="truncate text-sm text-zinc-200">{m.name}</div>
                      <div className="text-xs text-zinc-500">
                        {m.kind === "cpp" ? "dlls/main.dll · native code" : "Scripts/main.lua"} · {fmtBytes(m.size)}
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-3">
                    <Badge tone={m.kind === "cpp" ? "amber" : "blue"}>{m.kind === "cpp" ? "C++" : "Lua"}</Badge>
                    <Toggle label="" checked={m.enabled} disabled={disabled} onChange={(v) => setEnabled(m.name, v)} />
                    <Button
                      variant="ghost"
                      aria-label={`Delete ${m.name}`}
                      disabled={disabled}
                      onClick={() => confirm(`Delete ${m.name}?`) && run("delete", () => api(`/ue4ss/mods/${encodeURIComponent(m.name)}`, { method: "DELETE" }))}
                    >
                      <Trash2 size={15} />
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
          <div className="mt-3 border-t border-zinc-800 pt-3">
            <button onClick={() => setShowBuiltin(!showBuiltin)} className="flex w-full items-center justify-between text-left text-sm text-zinc-300 hover:text-zinc-100" aria-expanded={showBuiltin}>
              <span>
                Built-in mods
                <span className="ml-2 text-xs text-zinc-500">
                  {builtinMods.filter((m) => m.enabled).length} of {builtinMods.length} on · only the blueprint loader is needed on a dedicated server
                </span>
              </span>
              <span className="text-xs text-zinc-500">{showBuiltin ? "Hide" : "Show"}</span>
            </button>
            {showBuiltin && (
              <ul className="mt-2 space-y-2">
                {builtinMods.map((m) => (
                  <li key={m.name} className="flex items-center justify-between gap-3 text-sm text-zinc-400">
                    {m.name}
                    <Toggle label="" checked={m.enabled} disabled={disabled} onChange={(v) => setEnabled(m.name, v)} />
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Card>
      )}

      {status.installed && (
        <Card title="UE4SS log" desc="ue4ss/UE4SS.log, written each time the server starts." actions={<Button onClick={reloadLog}>Refresh</Button>}>
          <Console lines={log ?? []} className="h-64" />
        </Card>
      )}
    </div>
  );
}
