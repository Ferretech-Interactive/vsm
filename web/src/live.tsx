import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type ManagerEvent, type Status } from "./api";

interface Live {
  status: Status | null;
  log: string[];
  steam: string[];
  events: ManagerEvent[];
  connected: boolean;
}

const MAX_LINES = 2000;
const LiveContext = createContext<Live>({ status: null, log: [], steam: [], events: [], connected: false });

export const useLive = () => useContext(LiveContext);

function append<T>(list: T[], items: T[], max = MAX_LINES): T[] {
  const next = list.concat(items);
  return next.length > max ? next.slice(next.length - max) : next;
}

/** Holds the WebSocket and the rolling buffers every page reads from. */
export function LiveProvider({ children }: { children: ReactNode }) {
  const [live, setLive] = useState<Live>({ status: null, log: [], steam: [], events: [], connected: false });
  const pending = useRef<{ log: string[]; steam: string[]; events: ManagerEvent[] }>({ log: [], steam: [], events: [] });
  const version = useRef("");

  useEffect(() => {
    let socket: WebSocket | null = null;
    let closed = false;
    let retry = 1000;
    let timer: number | undefined;

    // Log lines can arrive in bursts; batch them into one render per frame.
    const flush = window.setInterval(() => {
      const p = pending.current;
      if (!p.log.length && !p.steam.length && !p.events.length) return;
      pending.current = { log: [], steam: [], events: [] };
      setLive((l) => ({ ...l, log: append(l.log, p.log), steam: append(l.steam, p.steam), events: append(l.events, p.events, 500) }));
    }, 200);

    const connect = async () => {
      try {
        const snap = await api<{ log: string[]; steam: string[]; events: ManagerEvent[] }>("/logs");
        if (closed) return;
        setLive((l) => ({ ...l, log: snap.log, steam: snap.steam, events: snap.events }));
      } catch {
        /* retried below via the socket closing */
      }
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${location.host}/api/ws`);
      socket.onopen = () => {
        retry = 1000;
        setLive((l) => ({ ...l, connected: true }));
      };
      socket.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === "status") {
          // The manager was updated under this page: load the new version's UI.
          if (version.current && msg.data.version && msg.data.version !== version.current) location.reload();
          version.current = msg.data.version;
          setLive((l) => ({ ...l, status: msg.data }));
        }
        else if (msg.type === "log") pending.current.log.push(msg.data);
        else if (msg.type === "steam") pending.current.steam.push(msg.data);
        else if (msg.type === "event") pending.current.events.push(msg.data);
      };
      socket.onclose = (e) => {
        setLive((l) => ({ ...l, connected: false }));
        if (closed) return;
        if (e.code === 4401) {
          window.dispatchEvent(new Event("vsm:unauthorized"));
          return;
        }
        timer = window.setTimeout(connect, retry);
        retry = Math.min(retry * 2, 15000);
      };
    };
    connect();

    return () => {
      closed = true;
      window.clearInterval(flush);
      window.clearTimeout(timer);
      socket?.close();
    };
  }, []);

  return <LiveContext.Provider value={live}>{children}</LiveContext.Provider>;
}
