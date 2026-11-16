import clsx from "clsx";
import { Check, ChevronsUpDown, Pencil, Plus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { Project } from "../../api/types";

/** `side`: full-width field in the sidebar; `rail`: the initial only; `bar`: the phone's top bar. */
export function ProjectSwitcher({
  projects,
  current,
  onSelect,
  onNew,
  onEdit,
  variant = "side",
}: {
  projects: Project[];
  current: Project;
  onSelect: (id: number) => void;
  onNew: () => void;
  onEdit: () => void;
  variant?: "side" | "rail" | "bar";
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const item = "flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left hover:bg-sunk touch:py-3";
  return (
    <div ref={ref} className={clsx("relative", variant === "bar" && "min-w-0")}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        title={variant === "rail" ? current.name : undefined}
        onClick={() => setOpen(!open)}
        className={clsx(
          "flex items-center gap-1.5 rounded-sm hover:bg-sunk",
          variant === "side" && "h-8 w-full px-2 font-semibold ring-1 ring-line-2 ring-inset",
          variant === "rail" && "size-[34px] justify-center font-extrabold ring-1 ring-line-2 ring-inset",
          variant === "bar" && "h-10 max-w-full px-2 font-extrabold",
        )}
      >
        {variant === "rail" ? (
          current.name[0]?.toUpperCase()
        ) : (
          <>
            <span className="truncate">{current.name}</span>
            <ChevronsUpDown className={clsx("size-3.5 shrink-0 text-fg-3", variant === "side" && "ml-auto")} />
          </>
        )}
      </button>
      {open && (
        <div role="menu" data-part="menu" className="absolute top-full left-0 z-40 mt-1 w-64 max-w-[calc(100vw-1rem)] rounded-sm bg-bg p-1 shadow-lg ring-1 ring-line-2">
          {projects.map((p) => (
            <button
              key={p.id}
              type="button"
              role="menuitem"
              onClick={() => {
                onSelect(p.id);
                setOpen(false);
              }}
              className={clsx(item, p.id === current.id && "font-semibold")}
            >
              <span className="flex-1 truncate">{p.name}</span>
              {p.id === current.id && <Check className="size-3.5 shrink-0" />}
            </button>
          ))}
          <div className="my-1 border-t border-line" />
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              onEdit();
              setOpen(false);
            }}
            className={clsx(item, "text-fg-2")}
          >
            <Pencil className="size-3.5 shrink-0" /> {t("projects.settings")}
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              onNew();
              setOpen(false);
            }}
            className={clsx(item, "text-fg-2")}
          >
            <Plus className="size-3.5 shrink-0" /> {t("projects.new")}
          </button>
        </div>
      )}
    </div>
  );
}
