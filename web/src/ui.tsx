import { Lock } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
} from "react";
import { api, type Config } from "./api";
import { useLive } from "./live";

const cx = (...parts: (string | false | null | undefined)[]) => parts.filter(Boolean).join(" ");

// ---------- layout ----------

export function PageHeader({ title, desc, actions }: { title: string; desc?: string; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold text-zinc-50">{title}</h1>
        {desc && <p className="mt-1 text-sm text-zinc-400">{desc}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Card({ title, desc, actions, children, className }: { title?: string; desc?: string; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cx("rounded-xl border border-zinc-800 bg-zinc-900/60 p-5", className)}>
      {(title || actions) && (
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div>
            {title && <h2 className="text-sm font-semibold text-zinc-100">{title}</h2>}
            {desc && <p className="mt-0.5 text-xs text-zinc-400">{desc}</p>}
          </div>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export const Grid = ({ children }: { children: ReactNode }) => <div className="grid gap-4 sm:grid-cols-2">{children}</div>;

// ---------- controls ----------

type Variant = "default" | "primary" | "danger" | "ghost";
const VARIANTS: Record<Variant, string> = {
  default: "bg-zinc-800 text-zinc-100 hover:bg-zinc-700 border-zinc-700",
  primary: "bg-emerald-600 text-white hover:bg-emerald-500 border-emerald-500",
  danger: "bg-red-600/90 text-white hover:bg-red-500 border-red-500",
  ghost: "bg-transparent text-zinc-300 hover:bg-zinc-800 border-transparent",
};

export function Button({ variant = "default", className, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type="button"
      {...props}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-500",
        "disabled:cursor-not-allowed disabled:opacity-50",
        VARIANTS[variant],
        className,
      )}
    />
  );
}

const FIELD = "w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-1.5 text-sm text-zinc-100 placeholder:text-zinc-600 focus:border-emerald-500 focus:outline-none disabled:opacity-50";

export const Input = ({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) => <input {...props} className={cx(FIELD, className)} />;

export const Select = ({ className, ...props }: SelectHTMLAttributes<HTMLSelectElement>) => <select {...props} className={cx(FIELD, className)} />;

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-zinc-300">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-zinc-500">{hint}</span>}
    </label>
  );
}

export function Toggle({ checked, onChange, label, hint, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; hint?: string; disabled?: boolean }) {
  return (
    <label className={cx("flex items-start gap-3", disabled ? "opacity-50" : "cursor-pointer")}>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cx("mt-0.5 h-5 w-9 shrink-0 rounded-full p-0.5 transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-500", checked ? "bg-emerald-600" : "bg-zinc-700")}
      >
        <span className={cx("block h-4 w-4 rounded-full bg-white transition-transform", checked && "translate-x-4")} />
      </button>
      <span>
        <span className="block text-sm text-zinc-200">{label}</span>
        {hint && <span className="block text-xs text-zinc-500">{hint}</span>}
      </span>
    </label>
  );
}

export function Badge({ tone = "zinc", children }: { tone?: "zinc" | "green" | "red" | "amber" | "blue"; children: ReactNode }) {
  const tones = {
    zinc: "bg-zinc-800 text-zinc-300",
    green: "bg-emerald-500/15 text-emerald-400",
    red: "bg-red-500/15 text-red-400",
    amber: "bg-amber-500/15 text-amber-400",
    blue: "bg-sky-500/15 text-sky-400",
  };
  return <span className={cx("inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium", tones[tone])}>{children}</span>;
}

export const Empty = ({ children }: { children: ReactNode }) => <p className="py-6 text-center text-sm text-zinc-500">{children}</p>;

/** Shown on pages that edit the server's ini files, which cannot be changed while it runs. */
export function useLocked(): boolean {
  const { status } = useLive();
  return !!status && status.state !== "OFFLINE";
}

export function LockNotice() {
  if (!useLocked()) return null;
  return (
    <div className="mb-4 flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-300">
      <Lock size={15} className="shrink-0" />
      Stop the server to edit these settings. The server rewrites its config files on shutdown, so changes made while it runs would be lost.
    </div>
  );
}

// ---------- toasts ----------

interface ToastApi {
  ok: (message: string) => void;
  err: (error: unknown) => void;
}
const ToastContext = createContext<ToastApi>({ ok: () => {}, err: () => {} });
export const useToast = () => useContext(ToastContext);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<{ id: number; text: string; bad: boolean }[]>([]);
  const next = useRef(1);
  const push = useCallback((text: string, bad: boolean) => {
    const id = next.current++;
    setToasts((t) => [...t, { id, text, bad }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), bad ? 7000 : 3000);
  }, []);
  const value = useRef<ToastApi>({
    ok: (m) => push(m, false),
    err: (e) => push(e instanceof Error ? e.message : String(e), true),
  }).current;
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed right-4 bottom-4 z-50 flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={cx("rounded-lg border px-4 py-2.5 text-sm shadow-lg", t.bad ? "border-red-500/40 bg-red-950 text-red-200" : "border-emerald-500/40 bg-emerald-950 text-emerald-200")}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

// ---------- data hooks ----------

export function useLoad<T>(path: string) {
  const [data, setData] = useState<T | null>(null);
  // What the server last sent or accepted: `dirty` says whether `data` has been edited since.
  const saved = useRef<string | null>(null);
  const toast = useToast();
  const setSaved = useCallback((value: T) => {
    saved.current = JSON.stringify(value);
    setData(value);
  }, []);
  const reload = useCallback(() => {
    api<T>(path).then(setSaved).catch(toast.err);
  }, [path, toast, setSaved]);
  useEffect(reload, [reload]);
  const dirty = data !== null && saved.current !== null && JSON.stringify(data) !== saved.current;
  return { data, setData, setSaved, dirty, reload };
}

/** Manager config with per-field editing; `save` sends only the sections a page owns. */
export function useConfig() {
  const { data, setData, setSaved, dirty } = useLoad<Config>("/config");
  const toast = useToast();
  const set = (section: string, key: string, value: unknown) => setData((d) => (d ? { ...d, [section]: { ...d[section], [key]: value } } : d));
  const save = async (...sections: string[]) => {
    if (!data) return false;
    try {
      setSaved(await api<Config>("/config", { method: "PUT", body: Object.fromEntries(sections.map((s) => [s, data[s]])) }));
      toast.ok("Saved.");
      return true;
    } catch (e) {
      toast.err(e);
      return false;
    }
  };
  return { cfg: data, set, save, dirty };
}

// ---------- console ----------

const HIGHLIGHTS: [RegExp, string][] = [
  [/Fatal|CRITICAL/, "text-red-500"],
  [/Error|ERROR/, "text-red-400"],
  [/Warning/, "text-amber-400"],
  [/LogVeinChat/, "text-cyan-300"],
  [/Join|Name=/, "text-emerald-400"],
  [/Ended auth session/, "text-orange-300"],
  [/Saved|Success/, "text-lime-300"],
  [/Steam|Auth/, "text-sky-400"],
];

export function Console({ lines, className }: { lines: string[]; className?: string }) {
  const box = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  useEffect(() => {
    const el = box.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [lines]);
  return (
    <div
      ref={box}
      onScroll={(e) => {
        const el = e.currentTarget;
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      }}
      className={cx("overflow-auto rounded-lg border border-zinc-800 bg-black/60 p-3 font-mono text-xs leading-5 text-zinc-400", className)}
    >
      {lines.length === 0 && <span className="text-zinc-600">No output yet.</span>}
      {lines.map((line, i) => (
        <div key={i} className={cx("break-all whitespace-pre-wrap", HIGHLIGHTS.find(([re]) => re.test(line))?.[1])}>
          {line}
        </div>
      ))}
    </div>
  );
}
