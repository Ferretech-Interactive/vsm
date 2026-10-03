import { ArrowUpCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "./api";
import { useLive } from "./live";
import { useToast } from "./ui";

interface UpdateInfo {
  current: string;
  available: boolean;
  latest: string | null;
  url: string | null;
  prerelease: boolean;
  /** True in the desktop window on the installed host, for a signed release: the app can install it. */
  one_click?: boolean;
  /** True on a Linux manager when the release has a package it can install itself. */
  self_update?: boolean;
}

/** Shown beside it when Steam has a newer game server build than the one installed. */
export function GameUpdateNotice({ latest }: { latest: string | null }) {
  return (
    <a href="#/install" className="flex items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-xs text-amber-200 hover:bg-amber-500/20" title="Open Install / Update">
      <ArrowUpCircle size={14} />
      Game update available{latest ? ` (build ${latest})` : ""}
    </a>
  );
}

const LOOK = "flex items-center gap-1.5 rounded-lg border border-sky-500/40 bg-sky-500/10 px-2.5 py-1 text-xs text-sky-200 hover:bg-sky-500/20";

/**
 * A quiet header notice when a newer release exists. In the desktop app on the server's own PC it
 * is a button: the app then asks, downloads Setup, checks its signature and runs it. Everywhere
 * else it is a link to the release page.
 */
export function UpdateNotice() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [asked, setAsked] = useState(false);
  const toast = useToast();
  const { status } = useLive();
  const own = status?.manager_update;
  useEffect(() => {
    const load = () =>
      api<UpdateInfo>("/update")
        .then(setInfo)
        .catch(() => {});
    load();
    // A window left open for days should still hear about a release. The manager decides how
    // often GitHub is really asked (Server Settings); this only re-reads its answer.
    const timer = setInterval(load, 30 * 60 * 1000);
    return () => clearInterval(timer);
  }, []);
  if (own && (own.state === "working" || own.state === "restarting")) {
    return (
      <span className={LOOK} title="The game server keeps running.">
        <ArrowUpCircle size={14} className="animate-pulse" />
        Updating the manager: {own.step || "starting"}
      </span>
    );
  }
  if (!info?.available || !info.url) return null;
  if (info.self_update) {
    return (
      <button
        type="button"
        className={own?.state === "failed" ? LOOK.replace(/sky/g, "red") : LOOK}
        title={own?.state === "failed" ? `The last attempt failed: ${own.error}` : "Installs it on the server; the game server keeps running"}
        onClick={() => {
          if (!confirm(`Update the manager to ${info.latest} now?\n\nIt downloads and installs the new version on the server, then restarts itself for a few seconds. The game server keeps running and players stay connected.`)) return;
          api("/update/self", { method: "POST" }).catch(toast.err);
        }}
      >
        <ArrowUpCircle size={14} />
        {own?.state === "failed" ? `Update to ${info.latest} failed: try again` : `Update to ${info.latest}`}
      </button>
    );
  }
  if (info.one_click) {
    return (
      <button
        type="button"
        className={LOOK}
        disabled={asked}
        onClick={() => {
          setAsked(true);
          setTimeout(() => setAsked(false), 15000);
          api("/update/install", { method: "POST" })
            .then(() => toast.ok("Answer the question that is about to appear to install the update."))
            .catch(toast.err);
        }}
      >
        <ArrowUpCircle size={14} />
        Update to {info.latest}
      </button>
    );
  }
  return (
    <a href={info.url} target="_blank" rel="noreferrer" className={LOOK}>
      <ArrowUpCircle size={14} />
      {info.latest} available{info.prerelease ? " (beta)" : ""}
    </a>
  );
}
