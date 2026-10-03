import { useState } from "react";
import { api } from "../api";
import { useLive } from "../live";
import { SteamUserLabel, SteamUserPicker, useSteamUsers } from "../steam";
import { Badge, Button, Card, Empty, PageHeader, useLoad, useToast } from "../ui";

interface Known {
  id: string;
  name: string;
  first_seen: string;
  last_seen: string;
}

interface PlayersData {
  history: Known[];
  bans: string[];
}

export default function Players() {
  const { data, setData, reload } = useLoad<PlayersData>("/players");
  const { status } = useLive();
  const toast = useToast();
  const [picking, setPicking] = useState(false);
  const users = useSteamUsers(data?.bans ?? []);
  if (!data) return null;

  const online = new Set(status?.players.map((p) => p.id));
  const banned = new Set(data.bans);
  const setBans = (bans: string[]) => setData({ ...data, bans });
  const ban = (id: string, name?: string) => {
    if (!confirm(`Ban ${name ?? id}?`)) return;
    api<string[]>("/bans", { method: "POST", body: { steam_id: id } })
      .then((bans) => {
        setBans(bans);
        setPicking(false);
        toast.ok("Banned. Takes effect after the next server restart.");
      })
      .catch(toast.err);
  };
  const unban = (id: string) => api<string[]>(`/bans/${id}`, { method: "DELETE" }).then(setBans).catch(toast.err);

  return (
    <>
      <PageHeader title="Players" desc="Everyone who has joined, and the ban list." actions={<Button onClick={reload}>Refresh</Button>} />
      <div className="space-y-4">
        <Card title={`Known players (${data.history.length})`}>
          {data.history.length === 0 ? (
            <Empty>No players have joined yet.</Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs text-zinc-500">
                  <tr>
                    <th className="pb-2 font-medium">Name</th>
                    <th className="pb-2 font-medium">SteamID</th>
                    <th className="pb-2 font-medium">Last seen</th>
                    <th className="pb-2" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800">
                  {data.history.map((p) => (
                    <tr key={p.id}>
                      <td className="py-2 pr-3 text-zinc-200">
                        {p.name} {online.has(p.id) && <Badge tone="green">Online</Badge>} {banned.has(p.id) && <Badge tone="red">Banned</Badge>}
                      </td>
                      <td className="py-2 pr-3 font-mono text-xs text-zinc-400">{p.id}</td>
                      <td className="py-2 pr-3 text-zinc-400">{new Date(p.last_seen).toLocaleString()}</td>
                      <td className="py-2 text-right">
                        {banned.has(p.id) ? <Button onClick={() => unban(p.id)}>Unban</Button> : <Button variant="danger" onClick={() => ban(p.id, p.name)}>Ban</Button>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card
          title={`Ban list (${data.bans.length})`}
          desc="Stored in Game.ini. The server reads it at startup."
          actions={
            <Button variant="danger" onClick={() => setPicking(true)}>
              Ban a player
            </Button>
          }
        >
          {picking && <SteamUserPicker title="Ban a player" action="Ban" exclude={data.bans} onClose={() => setPicking(false)} onPick={(id) => ban(id, users[id]?.name)} />}
          {data.bans.length === 0 ? (
            <Empty>Nobody is banned.</Empty>
          ) : (
            <ul className="divide-y divide-zinc-800">
              {data.bans.map((id) => (
                <li key={id} className="flex items-center justify-between py-2 text-sm first:pt-0 last:pb-0">
                  <SteamUserLabel id={id} user={users[id]} />
                  <Button onClick={() => unban(id)}>Unban</Button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}
