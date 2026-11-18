import clsx from "clsx";
import { Check } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Button, ErrorText } from "./ui";

export interface SettingsSection {
  id: string;
  /** In the list of sections, and as the head of the section. */
  label: string;
  /** One or two sentences: what is decided here. */
  description: ReactNode;
  content: ReactNode;
}

/** Settings as a list of sections with one of them open: the list at the left where there is room,
 * as a row above the section where there is not. Every section starts with what it is about. */
export function SettingsLayout({ sections, current, onChange, label }: { sections: SettingsSection[]; current: string; onChange: (id: string) => void; label: string }) {
  const section = sections.find((s) => s.id === current) ?? sections[0];
  // Where the sections are a row that scrolls, the open one is in view.
  const nav = useRef<HTMLElement>(null);
  useEffect(() => {
    nav.current?.querySelector('[aria-current="page"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [section.id]);
  return (
    <div data-part="settings" className="@container flex min-h-0 flex-1 flex-col">
      <div className="flex min-h-0 flex-1 flex-col @2xl:flex-row">
        <nav
          ref={nav}
          aria-label={label}
          className="scroll-thin flex shrink-0 gap-px overflow-x-auto border-b border-line px-2 py-2 @2xl:w-48 @2xl:flex-col @2xl:overflow-x-visible @2xl:overflow-y-auto @2xl:border-r @2xl:border-b-0 @2xl:p-3"
        >
          {sections.map((s) => (
            <button
              key={s.id}
              type="button"
              aria-current={s.id === section.id ? "page" : undefined}
              onClick={() => onChange(s.id)}
              className={clsx(
                "flex h-8 shrink-0 items-center rounded-sm px-2 text-left whitespace-nowrap transition-colors touch:h-11 touch:px-3",
                // In the column a label that is too long wraps instead of being cut off.
                "@2xl:h-auto @2xl:min-h-8 @2xl:py-1.5 @2xl:whitespace-normal touch:@2xl:min-h-11",
                s.id === section.id ? "plate" : "text-fg-2 hover:bg-sunk hover:text-fg",
              )}
            >
              {s.label}
            </button>
          ))}
        </nav>
        <div className="scroll-thin min-h-0 min-w-0 flex-1 overflow-y-auto">
          <section key={section.id} aria-label={section.label} className="mx-auto w-full max-w-2xl space-y-5 px-4 py-5 @2xl:px-8 @2xl:py-7">
            <header className="space-y-2">
              <h2 className="caps rule-b pb-1.5">{section.label}</h2>
              <p className="text-fg-2">{section.description}</p>
            </header>
            {section.content}
          </section>
        </div>
      </div>
    </div>
  );
}

/** The end of a section that is a form: nothing is stored until this is pressed, and it says when it was. */
export function SaveRow({ dirty, onSave, children }: { dirty: boolean; onSave: () => Promise<unknown>; children?: ReactNode }) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await onSave();
      setSaved(true);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-2 border-t border-line pt-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="primary" onClick={save} loading={busy} disabled={!dirty}>
          {t("common.save")}
        </Button>
        {dirty ? (
          <span className="text-fg-3">{t("settingsPage.unsaved")}</span>
        ) : (
          saved && (
            <span role="status" className="flex items-center gap-1 text-ok">
              <Check className="size-3.5" /> {t("settingsPage.saved")}
            </span>
          )
        )}
        {children && <div className="ml-auto flex items-center gap-2">{children}</div>}
      </div>
      <ErrorText error={error} />
    </div>
  );
}

/** A note under a list that stores every change by itself. */
export function SavedAtOnce() {
  const { t } = useTranslation();
  return <p className="text-fg-3">{t("settingsPage.atOnce")}</p>;
}
