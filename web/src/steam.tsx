import { ChevronLeft, ChevronRight, Loader2, Plus, Search, User, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "./api";
import { Button, Input } from "./ui";

export interface SteamUser {
  name: string;
  avatar: string;
}

interface Found extends SteamUser {
  id: string;
}

const PAGE_SIZE = 6;

interface Known {
  id: string;
  name: string;
  last_seen: string;
}

/** Names for a list of SteamIDs. The manager fetches unknown ones in the background; this polls until it is done. */
export function useSteamUsers(ids: string[]): Record<string, SteamUser> {
  const [users, setUsers] = useState<Record<string, SteamUser>>({});
  const key = ids.join(",");
  useEffect(() => {
    if (!key) return;
    let timer: ReturnType<typeof setTimeout>;
    let stopped = false;
    const load = () =>
      api<{ users: Record<string, SteamUser>; loading: boolean }>("/steam/users", { method: "POST", body: { ids: key.split(",") } })
        .then((r) => {
          if (stopped) return;
          setUsers((old) => ({ ...old, ...r.users }));
          if (r.loading) timer = setTimeout(load, 1500);
        })
        .catch(() => undefined); // names are a nicety: the IDs are still shown
    load();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [key]);
  return users;
}

function Avatar({ src, size = 28 }: { src?: string; size?: number }) {
  return src ? (
    <img src={src} alt="" width={size} height={size} className="shrink-0 rounded-md" referrerPolicy="no-referrer" />
  ) : (
    <span className="flex shrink-0 items-center justify-center rounded-md bg-zinc-800 text-zinc-500" style={{ width: size, height: size }}>
      <User size={size * 0.6} />
    </span>
  );
}

export function SteamUserLabel({ id, user, size }: { id: string; user?: SteamUser; size?: number }) {
  return (
    <span className="flex min-w-0 items-center gap-2.5">
      <Avatar src={user?.avatar} size={size} />
      <span className="min-w-0">
        <span className="block truncate text-sm text-zinc-200">{user?.name || "Unknown player"}</span>
        <span className="block font-mono text-xs text-zinc-500">{id}</span>
      </span>
    </span>
  );
}

/** A dialog that finds a Steam account from a profile link, custom name or ID, or from players seen on this server. */
export function SteamUserPicker({ title, action, exclude, onPick, onClose }: { title: string; action: string; exclude: string[]; onPick: (id: string) => void; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [found, setFound] = useState<Found | null>(null);
  const [error, setError] = useState("");
  const [known, setKnown] = useState<Known[]>([]);
  const [page, setPage] = useState(0);
  const taken = new Set(exclude);

  useEffect(() => {
    api<{ history: Known[] }>("/players")
      .then((r) => setKnown(r.history))
      .catch(() => undefined);
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const lookUp = () => {
    setBusy(true);
    setError("");
    setFound(null);
    api<Found>(`/steam/users/lookup?q=${encodeURIComponent(query.trim())}`)
      .then(setFound)
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };
  const text = query.trim().toLowerCase();
  const seen = known.filter((k) => !taken.has(k.id) && (!text || k.name.toLowerCase().includes(text) || k.id.includes(text)));
  const pages = Math.max(1, Math.ceil(seen.length / PAGE_SIZE));
  const current = Math.min(page, pages - 1);
  const shown = seen.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 pt-[10vh]" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div role="dialog" aria-label={title} className="w-full max-w-lg rounded-xl border border-zinc-700 bg-zinc-900 p-5 shadow-2xl">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold text-zinc-50">{title}</h2>
          <button type="button" onClick={onClose} aria-label="Close" className="rounded p-1 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100">
            <X size={18} />
          </button>
        </div>

        {/* Not a <form>: this dialog can sit inside a page's own form. */}
        <div className="flex gap-2">
          <Input
            autoFocus
            value={query}
            placeholder="Steam profile link, custom name or SteamID"
            aria-label="Steam profile link, custom name or SteamID"
            onChange={(e) => {
              setQuery(e.target.value);
              setPage(0);
            }}
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              e.preventDefault();
              if (query.trim() && !busy) lookUp();
            }}
          />
          <Button variant="primary" className="shrink-0 whitespace-nowrap" onClick={lookUp} disabled={!query.trim() || busy}>
            {busy ? <Loader2 size={15} className="animate-spin" /> : <Search size={15} />}
            Look up
          </Button>
        </div>
        <p className="mt-2 text-xs text-zinc-500">
          In Steam, open the player's profile and copy its address (right-click the page &gt; Copy Page URL), for example{" "}
          <span className="font-mono text-zinc-400">steamcommunity.com/id/name</span>. Your own: Steam &gt; your name &gt; Profile.
        </p>

        {error && <p className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-200">{error}</p>}
        {found && (
          <div className="mt-3 flex items-center justify-between gap-3 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2.5">
            <SteamUserLabel id={found.id} user={found} size={36} />
            {taken.has(found.id) ? (
              <span className="text-xs text-zinc-400">Already in the list</span>
            ) : (
              <Button variant="primary" onClick={() => onPick(found.id)}>
                {action}
              </Button>
            )}
          </div>
        )}

        {seen.length > 0 && (
          <>
            <div className="mt-5 mb-1.5 flex items-center justify-between text-xs font-medium text-zinc-500">
              <span>Seen on this server{text ? " (matching)" : ""}</span>
              <span className="tabular-nums">
                {current * PAGE_SIZE + 1}–{current * PAGE_SIZE + shown.length} of {seen.length}
              </span>
            </div>
            <ul className="divide-y divide-zinc-800 rounded-lg border border-zinc-800">
              {shown.map((k) => (
                <li key={k.id} className="flex items-center justify-between gap-3 px-3 py-2">
                  <SteamUserLabel id={k.id} user={{ name: k.name, avatar: "" }} />
                  <Button onClick={() => onPick(k.id)}>{action}</Button>
                </li>
              ))}
            </ul>
            {pages > 1 && (
              <div className="mt-2 flex items-center justify-between">
                <Button variant="ghost" disabled={current === 0} onClick={() => setPage(current - 1)}>
                  <ChevronLeft size={15} />
                  Previous
                </Button>
                <span className="text-xs text-zinc-500 tabular-nums">
                  Page {current + 1} of {pages}
                </span>
                <Button variant="ghost" disabled={current >= pages - 1} onClick={() => setPage(current + 1)}>
                  Next
                  <ChevronRight size={15} />
                </Button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/** An editable list of Steam accounts: each shown by name with a remove button, plus an "add" dialog. */
export function SteamIdList({ ids, onChange, addLabel = "Add player", pickerTitle = "Add a player", empty = "Nobody yet." }: { ids: string[]; onChange: (ids: string[]) => void; addLabel?: string; pickerTitle?: string; empty?: string }) {
  const users = useSteamUsers(ids);
  const [adding, setAdding] = useState(false);
  return (
    <div>
      {ids.length === 0 ? (
        <p className="mb-2 text-sm text-zinc-500">{empty}</p>
      ) : (
        <ul className="mb-2 space-y-1.5">
          {ids.map((id) => (
            <li key={id} className="flex items-center justify-between gap-2 rounded-lg border border-zinc-800 bg-zinc-950 py-1.5 pr-1.5 pl-2.5">
              <SteamUserLabel id={id} user={users[id]} />
              <button
                type="button"
                onClick={() => onChange(ids.filter((i) => i !== id))}
                aria-label={`Remove ${users[id]?.name || id}`}
                title="Remove"
                className="rounded p-1.5 text-zinc-500 hover:bg-zinc-800 hover:text-red-300"
              >
                <X size={16} />
              </button>
            </li>
          ))}
        </ul>
      )}
      <Button onClick={() => setAdding(true)}>
        <Plus size={15} />
        {addLabel}
      </Button>
      {adding && (
        <SteamUserPicker
          title={pickerTitle}
          action="Add"
          exclude={ids}
          onClose={() => setAdding(false)}
          onPick={(id) => {
            onChange([...ids, id]);
            setAdding(false);
          }}
        />
      )}
    </div>
  );
}
