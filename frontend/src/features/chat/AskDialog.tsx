import { ArrowUp } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { Markdown } from "../../components/Markdown";
import { Modal, Spinner } from "../../components/ui";

interface Entry {
  question: string;
  answer: string;
  /** Look-ups so far, e.g. "Read app/main.py". */
  steps: string[];
  state: "asking" | "done" | "error";
  error?: string;
}

type Line =
  | { type: "thread"; id: string; model: string }
  | { type: "delta"; text: string }
  | { type: "tool"; name: string; detail: string }
  | { type: "done"; text: string }
  | { type: "error"; message: string }
  | { type: "ping" };

/** A short question about the project, answered beside the chat by a read-only agent of its own. Nobody
 * else sees it, and the manager neither waits for it nor learns of it. Closing the window ends it. */
export function AskDialog({ projectId, question, onClose }: { projectId: number; question: string; onClose: () => void }) {
  const { t } = useTranslation();
  const [entries, setEntries] = useState<Entry[]>([]);
  const [model, setModel] = useState("");
  const [text, setText] = useState("");
  const thread = useRef<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const started = useRef(false);
  const area = useRef<HTMLTextAreaElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const busy = entries.some((e) => e.state === "asking");

  const update = (patch: (e: Entry) => Entry) => setEntries((all) => all.map((e, i) => (i === all.length - 1 ? patch(e) : e)));

  const ask = async (value: string) => {
    setEntries((all) => [...all, { question: value, answer: "", steps: [], state: "asking" }]);
    const controller = (abort.current = new AbortController());
    try {
      const resp = await fetch(`/api/projects/${projectId}/questions`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: value, thread: thread.current }),
        signal: controller.signal,
      });
      if (!resp.ok || !resp.body) {
        let detail = resp.statusText;
        try {
          detail = (await resp.json()).detail ?? detail;
        } catch {
          /* not json */
        }
        throw new Error(String(detail));
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let ended = false;
      for (;;) {
        const { value: chunk, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(chunk, { stream: true });
        let cut: number;
        while ((cut = buffer.indexOf("\n")) >= 0) {
          const raw = buffer.slice(0, cut).trim();
          buffer = buffer.slice(cut + 1);
          if (!raw) continue;
          const line = JSON.parse(raw) as Line;
          if (line.type === "thread") {
            thread.current = line.id;
            setModel(line.model);
          } else if (line.type === "delta") update((e) => ({ ...e, answer: e.answer + line.text }));
          // What was written before a look-up was a step on the way, not the answer.
          else if (line.type === "tool") update((e) => ({ ...e, answer: "", steps: [...e.steps, [line.name, line.detail].filter(Boolean).join(" ")] }));
          else if (line.type === "done") {
            ended = true;
            update((e) => ({ ...e, answer: line.text || e.answer, state: "done" }));
          } else if (line.type === "error") {
            ended = true;
            update((e) => ({ ...e, state: "error", error: line.message }));
          }
        }
      }
      if (!ended) update((e) => ({ ...e, state: "error", error: t("ask.broken") }));
    } catch (err) {
      if (controller.signal.aborted) return;
      update((e) => ({ ...e, state: "error", error: err instanceof Error ? err.message : String(err) }));
    }
  };

  useEffect(() => {
    if (started.current) return; // React's double effect in development asks once
    started.current = true;
    void ask(question);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [entries]);

  useLayoutEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [text]);

  const close = () => {
    abort.current?.abort();
    if (thread.current) void api.del(`/projects/${projectId}/questions/${thread.current}`).catch(() => undefined);
    onClose();
  };

  const followUp = () => {
    const value = text.trim();
    if (!value || busy) return;
    setText("");
    void ask(value);
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    const touch = window.matchMedia("(pointer: coarse)").matches;
    if (e.key === "Enter" && !e.shiftKey && (!touch || e.ctrlKey || e.metaKey) && !e.nativeEvent.isComposing) {
      e.preventDefault();
      followUp();
    }
  };

  return (
    <Modal
      open
      onClose={close}
      width="max-w-2xl"
      title={
        <span className="flex items-baseline gap-2">
          <span>{t("ask.title")}</span>
          {model && <span className="truncate font-normal text-fg-3">{model}</span>}
        </span>
      }
      footer={
        <div data-part="ask-composer" className="flex w-full items-end gap-1 rounded-sm p-1 ring-1 ring-line-2 ring-inset focus-within:ring-2 focus-within:ring-fg">
          <textarea
            ref={area}
            rows={1}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKey}
            placeholder={t("ask.followUp")}
            aria-label={t("ask.followUp")}
            className="scroll-thin max-h-[160px] min-h-8 min-w-0 flex-1 resize-none bg-transparent px-1.5 py-1.5 outline-none touch:min-h-11 touch:py-2.5"
          />
          <button
            onClick={followUp}
            disabled={!text.trim() || busy}
            aria-label={t("ask.send")}
            className="plate flex size-8 shrink-0 items-center justify-center rounded-sm transition-colors hover:bg-fg-2 disabled:bg-line disabled:text-fg-3 touch:size-11"
          >
            <ArrowUp className="size-4" />
          </button>
        </div>
      }
    >
      <div data-part="ask" className="space-y-4">
        <p className="text-fg-3">{t("ask.private")}</p>
        {entries.map((entry, i) => (
          <section key={i} className="space-y-2 border-t border-line pt-3">
            <p className="font-semibold whitespace-pre-wrap">{entry.question}</p>
            {entry.state === "asking" && (
              <div role="status" className="flex items-center gap-2 text-fg-2">
                <Spinner className="size-4" />
                <span className="min-w-0 truncate">{entry.steps.length ? entry.steps[entry.steps.length - 1] : t("ask.thinking")}</span>
              </div>
            )}
            {entry.steps.length > 0 && entry.state !== "asking" && (
              <details className="text-fg-3">
                <summary className="cursor-pointer">{t("ask.steps", { count: entry.steps.length })}</summary>
                <ul className="mt-1 space-y-0.5 font-mono">
                  {entry.steps.map((step, j) => (
                    <li key={j} className="truncate">
                      {step}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {entry.answer && <Markdown>{entry.answer}</Markdown>}
            {entry.state === "error" && <p className="text-you-fg">{entry.error}</p>}
          </section>
        ))}
        <div ref={bottom} />
      </div>
    </Modal>
  );
}
