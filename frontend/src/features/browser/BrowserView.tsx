import clsx from "clsx";
import { AppWindow, ArrowLeft, ArrowRight, ExternalLink, Keyboard, LayoutList, RotateCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ClipboardEvent, type FormEvent, type KeyboardEvent, type PointerEvent } from "react";
import { useTranslation } from "react-i18next";
import { Button, EmptyState, IconButton, Input, Spinner } from "../../components/ui";
import { previewPath } from "../../lib/preview";
import { useShell } from "../layout/shell-context";
import { PreviewList } from "./Previews";

/** An address to open, and a counter so that the same address can be asked for again. */
export interface BrowserRequest {
  url: string;
  n: number;
}

interface PageState {
  url: string;
  title: string;
  loading: boolean;
  back: boolean;
  forward: boolean;
}

interface PageDialog {
  kind: string;
  message: string;
  text: string;
}

const NO_PAGE: PageState = { url: "", title: "", loading: false, back: false, forward: false };
const RETRIES = 3;
const mods = (e: { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }) => (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);

/** A browser that runs next to the agents and is shown here: a page of an agent's server is at
 * localhost:<port> there, as it believes, so pages work that break behind /preview/<port>/.
 * This view is the picture and the hands: frames come in, what the user does goes back
 * (the messages are listed in the runner's browser.py). One browser per project. */
export function BrowserView({ request }: { request: BrowserRequest }) {
  const { t } = useTranslation();
  const { project, previews, canWrite } = useShell();
  const [listOpen, setListOpen] = useState(false); // the running previews, over a page that is open
  const frame = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const keys = useRef<HTMLTextAreaElement>(null);
  const socket = useRef<WebSocket | null>(null);
  const size = useRef({ w: 0, h: 0 });
  const asked = useRef(0);
  const [status, setStatus] = useState<"connecting" | "open" | "closed">("connecting");
  const [attempt, setAttempt] = useState(0);
  const [page, setPage] = useState<PageState>(NO_PAGE);
  const [painted, setPainted] = useState(false);
  const [address, setAddress] = useState(request.url);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState("");
  const [dialog, setDialog] = useState<PageDialog | null>(null);
  const [answer, setAnswer] = useState("");
  const touch = typeof window !== "undefined" && window.matchMedia("(pointer: coarse)").matches;

  const send = useCallback((message: Record<string, unknown>) => {
    if (socket.current?.readyState === WebSocket.OPEN) socket.current.send(JSON.stringify(message));
  }, []);

  const sendSize = useCallback(() => {
    const box = frame.current;
    if (!box || box.clientWidth < 50 || box.clientHeight < 50) return;
    size.current = { w: Math.round(box.clientWidth), h: Math.round(box.clientHeight) };
    send({ t: "size", ...size.current, dpr: Math.min(2, window.devicePixelRatio || 1), touch });
  }, [send, touch]);

  // The connection: frames as bytes, everything else as JSON.
  useEffect(() => {
    const ws = new WebSocket(`${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/api/projects/${project.id}/browser`);
    ws.binaryType = "arraybuffer";
    socket.current = ws;
    let newest = 0;
    let retry: number | undefined;
    ws.onopen = () => {
      setStatus("open");
      sendSize();
      if (request.url && asked.current !== request.n) {
        asked.current = request.n;
        send({ t: "nav", url: request.url });
      }
    };
    ws.onmessage = async (event) => {
      if (typeof event.data !== "string") {
        const mine = ++newest;
        send({ t: "ack" }); // it has arrived: the browser makes the next one now, not earlier (no frames queue up on a slow line)
        const bitmap = await createImageBitmap(new Blob([event.data], { type: "image/jpeg" }));
        const target = canvas.current;
        if (mine === newest && target) {
          if (target.width !== bitmap.width || target.height !== bitmap.height) {
            target.width = bitmap.width;
            target.height = bitmap.height;
          }
          target.getContext("2d")?.drawImage(bitmap, 0, 0);
          setPainted(true);
        }
        bitmap.close();
        return;
      }
      const message = JSON.parse(event.data);
      if (message.t === "state") {
        setPage(message);
        setError("");
      } else if (message.t === "dialog") {
        setDialog(message);
        setAnswer(message.text ?? "");
      } else if (message.t === "error") setError(message.message);
    };
    ws.onclose = () => {
      setStatus("closed");
      if (attempt < RETRIES) retry = window.setTimeout(() => setAttempt(attempt + 1), 1500);
    };
    return () => {
      window.clearTimeout(retry);
      ws.onclose = null;
      ws.close();
    };
    // `request` is read when the connection opens; a new request on an open connection is the effect below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id, attempt, send, sendSize]);

  // A new address asked for from outside (a preview link in the chat).
  useEffect(() => {
    if (!request.url || asked.current === request.n || socket.current?.readyState !== WebSocket.OPEN) return;
    asked.current = request.n;
    send({ t: "nav", url: request.url });
  }, [request, send]);

  // The page is as large as the room it has here.
  useEffect(() => {
    const box = frame.current;
    if (!box) return;
    let timer: number | undefined;
    const observer = new ResizeObserver(() => {
      window.clearTimeout(timer);
      timer = window.setTimeout(sendSize, 150);
    });
    observer.observe(box);
    return () => {
      window.clearTimeout(timer);
      observer.disconnect();
    };
  }, [sendSize]);

  useEffect(() => {
    if (!editing) setAddress(page.url === "about:blank" ? "" : page.url);
  }, [page.url, editing]);

  // The wheel must not scroll PlainBoard: React listens passively, so this one is set by hand.
  useEffect(() => {
    const target = canvas.current;
    if (!target) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const unit = e.deltaMode === 1 ? 40 : 1;
      const rect = target.getBoundingClientRect();
      send({ t: "wheel", x: ((e.clientX - rect.left) * size.current.w) / rect.width, y: ((e.clientY - rect.top) * size.current.h) / rect.height, dx: e.deltaX * unit, dy: e.deltaY * unit, mods: mods(e) });
    };
    target.addEventListener("wheel", onWheel, { passive: false });
    return () => target.removeEventListener("wheel", onWheel);
  }, [send]);

  const at = (e: { clientX: number; clientY: number }) => {
    const rect = canvas.current!.getBoundingClientRect();
    return { x: ((e.clientX - rect.left) * size.current.w) / rect.width, y: ((e.clientY - rect.top) * size.current.h) / rect.height };
  };
  const fingers = useRef(new Map<number, { x: number; y: number; id: number }>());
  const clicks = useRef({ at: 0, x: 0, y: 0, n: 0 });
  const moving = useRef<number | undefined>(undefined);
  const pending = useRef<Record<string, unknown> | null>(null);
  /** Moves are sent once per frame at most: the newest one is the one that counts. */
  const sendMove = (message: Record<string, unknown>) => {
    pending.current = message;
    moving.current ??= requestAnimationFrame(() => {
      moving.current = undefined;
      if (pending.current) send(pending.current);
      pending.current = null;
    });
  };

  const onPointerDown = (e: PointerEvent<HTMLCanvasElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    e.currentTarget.focus(); // keys go to the page from now on, also after a tap (a tablet with a keyboard)
    const point = at(e);
    if (e.pointerType === "touch") {
      fingers.current.set(e.pointerId, { ...point, id: e.pointerId });
      send({ t: "touch", type: "start", points: [...fingers.current.values()] });
      return;
    }
    const last = clicks.current;
    const again = e.timeStamp - last.at < 400 && Math.abs(point.x - last.x) < 5 && Math.abs(point.y - last.y) < 5;
    clicks.current = { at: e.timeStamp, ...point, n: again ? last.n + 1 : 1 };
    send({ t: "mouse", type: "down", ...point, button: e.button, buttons: e.buttons, clicks: clicks.current.n, mods: mods(e) });
  };
  const onPointerMove = (e: PointerEvent<HTMLCanvasElement>) => {
    const point = at(e);
    if (e.pointerType === "touch") {
      if (!fingers.current.has(e.pointerId)) return;
      fingers.current.set(e.pointerId, { ...point, id: e.pointerId });
      sendMove({ t: "touch", type: "move", points: [...fingers.current.values()] });
    } else sendMove({ t: "mouse", type: "move", ...point, buttons: e.buttons, mods: mods(e) });
  };
  const onPointerUp = (e: PointerEvent<HTMLCanvasElement>) => {
    if (e.pointerType === "touch") {
      fingers.current.delete(e.pointerId);
      const rest = [...fingers.current.values()];
      send(rest.length ? { t: "touch", type: "move", points: rest } : { t: "touch", type: e.type === "pointercancel" ? "cancel" : "end", points: [] });
      return;
    }
    send({ t: "mouse", type: "up", ...at(e), button: e.button, buttons: e.buttons, clicks: clicks.current.n, mods: mods(e) });
  };

  const onKey = (e: KeyboardEvent<HTMLCanvasElement>) => {
    const command = e.ctrlKey || e.metaKey;
    if (command && e.key.toLowerCase() === "v") return; // the paste event brings the text
    e.preventDefault();
    const text = e.type === "keydown" && !command ? (e.key.length === 1 ? e.key : e.key === "Enter" ? "\r" : "") : "";
    send({ t: "key", type: e.type === "keydown" ? "down" : "up", key: e.key, code: e.code, keyCode: e.keyCode, mods: mods(e), text });
  };
  const onPaste = (e: ClipboardEvent) => {
    const text = e.clipboardData.getData("text/plain");
    if (text) send({ t: "text", text });
    e.preventDefault();
  };
  const press = (key: string, keyCode: number, text = "") => {
    send({ t: "key", type: "down", key, code: key, keyCode, mods: 0, text });
    send({ t: "key", type: "up", key, code: key, keyCode, mods: 0, text: "" });
  };
  /** The on-screen keyboard types into a hidden field; what it enters is passed on and the field emptied. */
  const onSoftInput = (e: FormEvent<HTMLTextAreaElement>) => {
    const native = e.nativeEvent as InputEvent;
    const field = e.currentTarget;
    if (native.isComposing) return; // a word still being composed arrives with compositionend
    if (native.inputType?.startsWith("delete")) press("Backspace", 8);
    else if (native.inputType === "insertLineBreak" || native.data === "\n") press("Enter", 13, "\r");
    else if (native.data) send({ t: "text", text: native.data });
    field.value = "";
  };

  const go = (e: FormEvent) => {
    e.preventDefault();
    if (!address.trim()) return;
    open(address.trim());
    setEditing(false);
  };
  const settle = (accept: boolean) => {
    send({ t: "dialog", accept, text: answer });
    setDialog(null);
  };

  const tab = previewPath(page.url);
  const blank = !page.url || page.url === "about:blank";
  const noPage = blank || !painted;
  /** Shows an address of the agents' environment, e.g. a preview picked from the list. */
  const open = (url: string) => {
    setError("");
    setListOpen(false);
    send({ t: "nav", url });
    canvas.current?.focus();
  };

  return (
    <div className="flex h-full flex-col">
      <form data-part="page-head" onSubmit={go} className="flex h-12 shrink-0 items-center gap-1 border-b border-line px-2">
        <IconButton label={t("browser.back")} disabled={!page.back} onClick={() => send({ t: "back" })}>
          <ArrowLeft className="size-4" />
        </IconButton>
        <IconButton label={t("browser.forward")} disabled={!page.forward} onClick={() => send({ t: "forward" })} className="max-sm:hidden">
          <ArrowRight className="size-4" />
        </IconButton>
        <IconButton label={t("browser.reload")} disabled={blank} onClick={() => send({ t: "reload" })}>
          {page.loading ? <Spinner /> : <RotateCw className="size-4" />}
        </IconButton>
        <Input
          type="text"
          inputMode="url"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          onFocus={(e) => {
            setEditing(true);
            e.currentTarget.select();
          }}
          onBlur={() => setEditing(false)}
          placeholder={t("browser.address")}
          aria-label={t("browser.address")}
          title={page.title || undefined}
          className="mx-1 min-w-0 flex-1"
        />
        {previews.length > 0 && !noPage && (
          <IconButton label={t("previews.running")} aria-pressed={listOpen} onClick={() => setListOpen(!listOpen)} className={clsx(listOpen && "bg-sunk text-fg")}>
            <LayoutList className="size-4" />
          </IconButton>
        )}
        {touch && (
          <IconButton label={t("browser.keyboard")} onClick={() => keys.current?.focus()}>
            <Keyboard className="size-4" />
          </IconButton>
        )}
        {tab && (
          <a href={tab} target="_blank" rel="noreferrer" title={t("browser.openTab")} aria-label={t("browser.openTab")} className="inline-flex size-7 shrink-0 items-center justify-center rounded-sm text-fg-3 hover:bg-sunk hover:text-fg touch:size-11">
            <ExternalLink className="size-4" />
          </a>
        )}
      </form>
      {error && (
        <p data-part="error" role="alert" className="shrink-0 border-b border-line bg-you-bg px-4 py-2 text-you-fg">
          {error}
        </p>
      )}
      <div ref={frame} className="relative min-h-0 flex-1 overflow-hidden bg-sunk" onPaste={onPaste}>
        <canvas
          ref={canvas}
          tabIndex={0}
          aria-label={page.title || t("nav.browser")}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          onContextMenu={(e) => e.preventDefault()}
          onKeyDown={onKey}
          onKeyUp={onKey}
          className={clsx("block size-full touch-none outline-none", (blank || !painted) && "invisible")}
        />
        <textarea
          ref={keys}
          aria-hidden
          tabIndex={-1}
          autoCapitalize="off"
          autoCorrect="off"
          autoComplete="off"
          spellCheck={false}
          onInput={onSoftInput}
          onCompositionEnd={(e) => {
            if (e.data) send({ t: "text", text: e.data });
            e.currentTarget.value = "";
          }}
          className="pointer-events-none absolute top-0 left-0 size-px opacity-0"
        />
        {(noPage || listOpen) && (
          <div className="absolute inset-0 overflow-y-auto bg-sunk">
            {/* The start page: what runs right now, then what the window itself has to say. */}
            <div className="mx-auto flex min-h-full w-full max-w-2xl flex-col items-center justify-center gap-4 p-4">
              <PreviewList onOpenHere={status === "open" ? open : undefined} />
              {!noPage ? null : status === "connecting" ? (
                <p className="flex items-center gap-2 text-fg-2">
                  <Spinner /> {t("browser.connecting")}
                </p>
              ) : status === "closed" ? (
                // A viewer has no browser window: the list above is all there is for them.
                canWrite && (
                  <EmptyState title={t("browser.closed")}>
                    <Button className="mt-3" onClick={() => setAttempt(attempt + 1)}>
                      {t("browser.reconnect")}
                    </Button>
                  </EmptyState>
                )
              ) : previews.length ? (
                <p className="max-w-sm text-center text-fg-3">{t("browser.startHint")}</p>
              ) : (
                <EmptyState icon={<AppWindow className="size-8" />} title={t("browser.emptyTitle")}>
                  {t("browser.emptyHint")}
                </EmptyState>
              )}
            </div>
          </div>
        )}
        {status === "closed" && !noPage && (
          <div className="absolute inset-x-0 top-0 flex items-center justify-center gap-3 border-b border-line bg-bg px-4 py-2">
            <span className="text-fg-2">{t("browser.closed")}</span>
            <Button size="sm" onClick={() => setAttempt(attempt + 1)}>
              {t("browser.reconnect")}
            </Button>
          </div>
        )}
        {dialog && (
          <div className="absolute inset-0 flex items-start justify-center bg-black/40 p-4 pt-[10%]">
            <div role="alertdialog" aria-modal="true" data-part="modal" className="w-full max-w-sm rounded-sm bg-bg p-4 shadow-lg ring-1 ring-line-2">
              <p className="break-words whitespace-pre-wrap">{dialog.message || t("browser.leavePage")}</p>
              {dialog.kind === "prompt" && <Input autoFocus value={answer} onChange={(e) => setAnswer(e.target.value)} className="mt-3" aria-label={dialog.message} />}
              <div className="mt-4 flex justify-end gap-2">
                {dialog.kind !== "alert" && <Button onClick={() => settle(false)}>{t("common.cancel")}</Button>}
                <Button variant="primary" autoFocus={dialog.kind !== "prompt"} onClick={() => settle(true)}>
                  {t("browser.ok")}
                </Button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
