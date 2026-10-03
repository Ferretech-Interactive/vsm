import vsmLogo from "../assets/vsm-logo.png";
import { useLive } from "../live";
import { PageHeader } from "../ui";

const REPO = "https://github.com/Ferretech-Interactive/vsm";
const LINKS = [
  ["GitHub", REPO],
  ["Releases", REPO + "/releases"],
  ["MIT license", REPO + "/blob/main/LICENSE"],
  ["Discord", ""], // placeholder: put the invite link here
];

/** The same content as the desktop app's Help > About window, for browsers, which have no menu bar. */
export default function About() {
  const { status } = useLive();
  const kind = status?.service_mode ? "Installed: runs as a Windows service" : status ? `Running on ${status.platform === "windows" ? "Windows" : "Linux"}` : "";
  return (
    <>
      <PageHeader title="About" desc="Version and project links." />
      <div className="mx-auto max-w-md rounded-xl border border-zinc-800 bg-zinc-900/40 px-7 py-8 text-center">
        <img src={vsmLogo} alt="" className="mx-auto h-16 w-auto" />
        <h2 className="mt-4 text-xl font-semibold text-zinc-50">Vein Server Manager</h2>
        <div className="mt-0.5 font-mono text-sm text-zinc-400">Version {status?.version ?? "…"}</div>
        {kind && <div className="mt-2.5 inline-block rounded-md border border-zinc-800 bg-zinc-900 px-2.5 py-0.5 text-xs text-zinc-300">{kind}</div>}
        <p className="mt-5 text-sm leading-relaxed text-zinc-400">
          Published by{" "}
          <a href="https://ferretech.org" target="_blank" rel="noreferrer" className="text-sky-300 hover:underline">
            FerreTech Interactive
          </a>
          .
          <br />
          Based on the original Vein Server Manager by{" "}
          <a href="https://github.com/Wolverinex1974/Vein-Server-Manager" target="_blank" rel="noreferrer" className="text-sky-300 hover:underline">
            Wolverinex1974
          </a>
          .
        </p>
        <div className="mt-5 flex flex-wrap justify-center gap-x-5 gap-y-2 border-t border-zinc-800 pt-4 text-sm">
          {LINKS.map(([label, href]) =>
            href ? (
              <a key={label} href={href} target="_blank" rel="noreferrer" className="text-sky-300 hover:underline">
                {label}
              </a>
            ) : (
              <span key={label} title="Coming soon" className="cursor-default text-zinc-500">
                {label}
              </span>
            ),
          )}
        </div>
        <p className="mt-4 text-[11px] leading-relaxed text-zinc-500">
          An independent project, not affiliated with, endorsed by or connected to VEIN or Ramjet Studios. All trademarks and copyrights belong to their respective owners.
        </p>
      </div>
    </>
  );
}
