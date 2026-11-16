import clsx from "clsx";
import { Check } from "lucide-react";
import type { CSSProperties } from "react";
import { useTranslation } from "react-i18next";
import { Modal, Segmented } from "../../components/ui";
import { APPEARANCES, DESIGNS, setAppearance, setDesign, useLook } from "../../lib/theme";

/** Choose the design (the look) and whether it is light, dark or follows the system. Both apply
 * at once and are kept in this browser. */
export function LookDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const look = useLook();
  return (
    <Modal open={open} onClose={onClose} title={t("look.title")} width="max-w-lg">
      <div className="space-y-5">
        <section>
          <h2 className="caps mb-2">{t("look.design.label")}</h2>
          <div role="radiogroup" aria-label={t("look.design.label")} className="grid gap-2 sm:grid-cols-2">
            {DESIGNS.map((design) => {
              const chosen = design.id === look.design;
              return (
                <button
                  key={design.id}
                  type="button"
                  role="radio"
                  aria-checked={chosen}
                  onClick={() => setDesign(design.id)}
                  className={clsx("rounded-sm p-2.5 text-left ring-inset", chosen ? "ring-2 ring-fg" : "ring-1 ring-line-2 hover:bg-sunk")}
                >
                  <Swatch {...(look.dark ? design.dark : design.light)} />
                  <span className="mt-2 flex items-center gap-1.5 font-semibold">
                    <span className="min-w-0 flex-1 truncate">{t(`look.design.${design.id}`)}</span>
                    {chosen && <Check className="size-4 shrink-0" />}
                  </span>
                  <span className="block text-fg-2">{t(`look.design.${design.id}Hint`)}</span>
                </button>
              );
            })}
          </div>
        </section>
        <section>
          <h2 className="caps mb-2">{t("look.appearance.label")}</h2>
          <Segmented
            aria-label={t("look.appearance.label")}
            value={look.appearance}
            onChange={setAppearance}
            options={APPEARANCES.map((value) => ({ value, label: t(`look.appearance.${value}`) }))}
          />
        </section>
      </div>
    </Modal>
  );
}

/** A design in miniature, drawn with the colours of its own theme: a ticket that waits, two that
 * don't, and the strong element. */
function Swatch({ theme, accent }: { theme: string; accent: string }) {
  const bar = (width: string, color: string): CSSProperties => ({ width, height: 5, background: color });
  return (
    <span data-theme={theme} aria-hidden className="flex h-16 gap-2 overflow-hidden p-2" style={{ background: "var(--bg)", boxShadow: "inset 0 0 0 1px var(--line)" }}>
      <span className="flex flex-1 flex-col justify-between">
        <span className="block" style={bar("70%", "var(--fg)")} />
        <span className="block" style={bar("90%", "var(--fg-3)")} />
        <span className="block" style={bar("55%", "var(--fg-3)")} />
        <span className="block" style={bar("40%", "var(--you)")} />
      </span>
      <span className="block w-8 self-start" style={{ height: 14, background: accent }} />
    </span>
  );
}
