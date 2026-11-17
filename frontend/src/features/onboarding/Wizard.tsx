import clsx from "clsx";
import { Check, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Button, ErrorText, IconButton } from "../../components/ui";
import { Logo } from "../auth/Login";

export interface WizardStep {
  id: string;
  /** Short, for the list of steps. */
  label: string;
  /** The question this step answers. */
  title: string;
  intro: ReactNode;
  content: ReactNode;
  /** False: something on this step is still missing, and `missing` says what. */
  ready?: boolean;
  missing?: string;
  /** Runs when the user goes on; a failure keeps them on the step. */
  onNext?: () => Promise<void>;
  /** After such a failure the user may go on anyway (a check that can fail for reasons of its own). */
  canSkipFailure?: boolean;
}

/** A few steps, one decision each, on a page of its own: the list of steps, the current one, back and on.
 * Every step can be left only when it is ready, so nothing is skipped by accident. */
export function Wizard({ heading, steps, doneLabel, onDone, onClose }: { heading: string; steps: WizardStep[]; doneLabel: string; onDone: () => Promise<void> | void; onClose?: () => void }) {
  const { t } = useTranslation();
  const [index, setIndex] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const step = steps[Math.min(index, steps.length - 1)];
  const last = index >= steps.length - 1;
  const ready = step.ready !== false;

  const go = (to: number) => {
    setError(null);
    setIndex(to);
  };
  const next = async (check = true) => {
    setBusy(true);
    setError(null);
    try {
      if (check) await step.onNext?.();
      if (last) await onDone();
      else setIndex(index + 1);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-bg pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)]">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-line px-4">
        <Logo />
        <span className="min-w-0 flex-1 truncate font-extrabold">{heading}</span>
        <span className="text-fg-3 md:hidden">{t("wizard.stepOf", { step: index + 1, count: steps.length })}</span>
        {onClose && (
          <IconButton label={t("common.close")} onClick={onClose}>
            <X className="size-4" />
          </IconButton>
        )}
      </header>
      <div className="flex min-h-0 flex-1">
        <nav aria-label={t("wizard.steps")} className="hidden w-56 shrink-0 border-r border-line p-3 md:block">
          <ol className="space-y-px">
            {steps.map((s, i) => (
              <li key={s.id}>
                <button
                  type="button"
                  disabled={i > index}
                  aria-current={i === index ? "step" : undefined}
                  onClick={() => go(i)}
                  className={clsx("flex h-8 w-full items-center gap-2.5 rounded-sm px-2 text-left", i === index ? "plate" : i < index ? "text-fg-2 hover:bg-sunk hover:text-fg" : "text-fg-3")}
                >
                  <span className="flex size-4 shrink-0 items-center justify-center tabular-nums">{i < index ? <Check className="size-3.5" /> : i + 1}</span>
                  <span className="truncate">{s.label}</span>
                </button>
              </li>
            ))}
          </ol>
        </nav>
        <main className="scroll-thin min-w-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-2xl space-y-5 px-4 py-6 sm:px-8 sm:py-8">
            <div className="space-y-1.5">
              <h1 className="font-extrabold">{step.title}</h1>
              <p className="text-fg-2">{step.intro}</p>
            </div>
            {step.content}
            <ErrorText error={error} />
          </div>
        </main>
      </div>
      <footer className="flex shrink-0 flex-wrap items-center gap-2 border-t border-line px-4 py-3">
        {index > 0 && (
          <Button onClick={() => go(index - 1)} disabled={busy}>
            {t("wizard.back")}
          </Button>
        )}
        <div className="min-w-0 flex-1 text-right text-fg-3">{!ready && step.missing}</div>
        {error !== null && step.canSkipFailure && (
          <Button variant="ghost" onClick={() => next(false)} disabled={busy}>
            {t("wizard.anyway")}
          </Button>
        )}
        <Button variant="primary" onClick={() => next()} loading={busy} disabled={!ready}>
          {last ? doneLabel : t("wizard.next")}
        </Button>
      </footer>
    </div>
  );
}

/** What PlainBoard would choose, and why: next to the control it is about. */
export function Advice({ children, action, onAction }: { children: ReactNode; action?: string; onAction?: () => void }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-sm bg-sunk px-3 py-2 text-fg-2">
      <p className="min-w-0 flex-1">{children}</p>
      {action && onAction && (
        <Button size="sm" onClick={onAction}>
          {action}
        </Button>
      )}
    </div>
  );
}
