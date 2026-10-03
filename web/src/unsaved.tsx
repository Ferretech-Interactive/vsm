import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Button } from "./ui";

/** Pages with edits that have not been saved yet, and how to save each. */
const pending = new Map<symbol, () => Promise<boolean>>();

export const hasUnsaved = () => pending.size > 0;

/**
 * Tell the app this page has unsaved edits. Leaving the page (another page, an inner tab,
 * signing out) then asks first, and "Save changes" in that question calls `save`.
 * `save` should resolve to false when saving failed, so the person stays on the page.
 */
export function useUnsaved(dirty: boolean, save: () => Promise<unknown> | unknown) {
  const latest = useRef(save);
  latest.current = save;
  useEffect(() => {
    if (!dirty) return;
    const key = Symbol();
    pending.set(key, async () => (await latest.current()) !== false);
    return () => {
      pending.delete(key);
    };
  }, [dirty]);
}

const LeaveContext = createContext<(leave: () => void) => void>((leave) => leave());

/** `confirmLeave(action)` runs `action` at once when nothing is unsaved, otherwise asks first. */
export const useConfirmLeave = () => useContext(LeaveContext);

export function UnsavedProvider({ children }: { children: ReactNode }) {
  const [leave, setLeave] = useState<(() => void) | null>(null);
  const [saving, setSaving] = useState(false);
  const confirmLeave = useCallback((action: () => void) => {
    if (hasUnsaved()) setLeave(() => action);
    else action();
  }, []);

  // Closing the browser tab or reloading: the browser shows its own "leave site?" question.
  useEffect(() => {
    const onUnload = (e: BeforeUnloadEvent) => {
      if (hasUnsaved()) e.preventDefault();
    };
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, []);

  const stay = () => setLeave(null);
  const go = () => {
    const action = leave;
    pending.clear(); // discarded, or just saved
    setLeave(null);
    action?.();
  };
  const saveThenGo = async () => {
    setSaving(true);
    try {
      for (const save of [...pending.values()]) {
        if (!(await save())) return stay(); // the page shows why; keep the edits on screen
      }
      go();
    } finally {
      setSaving(false);
    }
  };

  return (
    <LeaveContext.Provider value={confirmLeave}>
      {children}
      {leave && (
        <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 p-4 pt-[18vh]">
          <div role="alertdialog" aria-labelledby="unsaved-title" className="w-full max-w-md rounded-xl border border-zinc-700 bg-zinc-900 p-5 shadow-2xl">
            <h2 id="unsaved-title" className="text-base font-semibold text-zinc-50">
              Save your changes?
            </h2>
            <p className="mt-2 text-sm text-zinc-400">This page has changes that have not been saved. If you leave without saving, they are lost.</p>
            <div className="mt-5 flex flex-wrap justify-end gap-2">
              <Button variant="ghost" onClick={stay} disabled={saving}>
                Keep editing
              </Button>
              <Button onClick={go} disabled={saving}>
                Discard changes
              </Button>
              <Button variant="primary" onClick={saveThenGo} disabled={saving} autoFocus>
                {saving ? "Saving…" : "Save changes"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </LeaveContext.Provider>
  );
}
