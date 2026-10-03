import { useEffect, useState } from "react";
import { api } from "./api";
import { Field, Select, useLoad } from "./ui";

interface Adapters {
  adapters: { name: string; address: string }[];
  automatic: { name: string; address: string | null; gateway: string | null };
}

interface Found {
  ok: boolean;
  device: string;
  double_nat: boolean;
  message: string;
}

/**
 * Looks for the UPnP router straight away and says what it found, with the choice of network
 * adapter beside it. Automatic is right unless a VPN or a second connection takes the internet
 * traffic. Looking changes nothing on the router: ports are only forwarded when the server runs.
 */
export function UpnpRouter({ value, onChange }: { value: string; onChange: (adapter: string) => void }) {
  const { data } = useLoad<Adapters>("/upnp/adapters");
  const [found, setFound] = useState<Found | null>(null);
  const [choosing, setChoosing] = useState(false);
  useEffect(() => {
    let current = true;
    setFound(null);
    api<Found>("/upnp/check", { method: "POST", body: { adapter: value } })
      .then((r) => current && setFound(r))
      .catch((e) => current && setFound({ ok: false, device: "", double_nat: false, message: String(e?.message ?? e) }));
    return () => {
      current = false;
    };
  }, [value]);
  if (!data) return null;
  const auto = data.automatic;
  const automatic = auto.address ? `Automatic: ${auto.name || auto.address}${auto.gateway ? `, router ${auto.gateway}` : ""}` : "Automatic";
  const missing = value && !data.adapters.some((a) => a.name === value);
  return (
    <div className="space-y-2">
      <div
        role="status"
        className={`rounded-lg border px-3 py-2 text-sm ${
          !found
            ? "border-zinc-800 text-zinc-400"
            : found.ok && !found.double_nat
              ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
              : "border-amber-500/30 bg-amber-500/10 text-amber-200"
        }`}
      >
        {!found
          ? "Looking for your router…"
          : found.ok
            ? found.double_nat
              ? `The router at ${found.device} answered, but its own address is not a public one: there is another router or your provider's NAT in front of it, so players may still be unable to connect.`
              : value
                ? `The device at ${found.device} answered and can forward the ports.`
                : `Your router (${found.device}) answered and can forward the ports.`
            : found.message}
      </div>
      {/* The choice matters when the search failed, or once somebody has made one. */}
      {value || choosing || (found && !found.ok) ? (
        <Field
          label="Network adapter the router is on"
          hint={
            value
              ? "The manager uses whichever UPnP device answers on this adapter's network; the line above says which."
              : "A VPN or a second connection can send the search the wrong way. Choose the adapter your router is on."
          }
        >
          <Select value={value} onChange={(e) => onChange(e.target.value)} className="max-w-md" aria-label="Network adapter the router is on">
            <option value="">{automatic}</option>
            {data.adapters.map((a) => (
              <option key={a.name + a.address} value={a.name}>
                {a.name} ({a.address})
              </option>
            ))}
            {missing && <option value={value}>{value} (not connected)</option>}
          </Select>
        </Field>
      ) : (
        found && (
          <button type="button" onClick={() => setChoosing(true)} className="text-xs text-zinc-400 underline hover:text-zinc-200">
            That is not my router: use a different network adapter
          </button>
        )
      )}
    </div>
  );
}
