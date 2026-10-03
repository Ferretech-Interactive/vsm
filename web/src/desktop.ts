/** Is this page open inside the Windows desktop app, rather than a browser?
 *
 * The app's window has no bridge into the page, so it says so in the address it opens
 * (`?desktop=1`). That is remembered for the session and taken out of the address bar.
 * It only decides what is worth showing (the app has its own Help > About), never access. */
const KEY = "vsm.desktop";

function detect(): boolean {
  try {
    if (new URLSearchParams(location.search).has("desktop")) {
      sessionStorage.setItem(KEY, "1");
      history.replaceState(null, "", location.pathname + location.hash);
    }
    return sessionStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

export const IN_DESKTOP_APP = detect();
