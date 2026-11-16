import clsx from "clsx";
import { AlertCircle, Archive, Bell, CalendarClock, ChevronRight, ClipboardList, HelpCircle, Mail, Paperclip, RefreshCw, Ticket, Wrench } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { Agent, Message } from "../../api/types";
import { AttachmentLink } from "../../components/AttachmentLink";
import { Lightbox } from "../../components/Lightbox";
import { Markdown, TicketText } from "../../components/Markdown";
import { chatTime, formatDateTime, formatTokens } from "../../lib/format";
import { useShell } from "../layout/shell-context";

/** Every entry of the chat is a ruled row with the same inset. */
export const CHAT_ROW = "border-b border-line px-4 py-2.5";
const LINK = "shrink-0 underline decoration-line-2 underline-offset-[3px] hover:decoration-current";

/** `details` shows tool activity, compression markers and full event cards; otherwise the chat
 * stays clean (messages only, events as one-line notes). */
export function MessageItem({ message, agent, details }: { message: Message; agent: Agent; details: boolean }) {
  switch (message.kind) {
    case "tool":
      return details ? <ToolLine message={message} /> : null;
    case "summary":
      return details ? <SummaryDivider message={message} /> : null;
    case "error":
      return (
        <div className={clsx(CHAT_ROW, "flex items-start gap-2 bg-you-bg text-you-fg")}>
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          <span className="min-w-0 break-words whitespace-pre-wrap">
            <TicketText text={message.content} />
          </span>
        </div>
      );
  }
  if (message.role === "user") return <UserMessage message={message} details={details} />;
  if (message.kind === "files")
    return (
      <div className={CHAT_ROW}>
        <Head name={agent.name} at={message.created_at} />
        <MessageFiles files={message.meta.attachments ?? []} />
      </div>
    );
  return (
    <div className={CHAT_ROW}>
      <Head name={agent.name} at={message.created_at} />
      <Markdown>{message.content}</Markdown>
    </div>
  );
}

/** Who wrote the row, and when: name at the left, time in a fixed place at the right. */
export function Head({ name, at, children }: { name: string; at?: string; children?: ReactNode }) {
  return (
    <div className="mb-0.5 flex items-baseline gap-2">
      <b className="min-w-0 truncate font-semibold">{name}</b>
      {children}
      {at && <MessageTime at={at} />}
    </div>
  );
}

function MessageTime({ at }: { at: string }) {
  return (
    <time dateTime={at} title={formatDateTime(at)} className="ml-auto shrink-0 pl-2 whitespace-nowrap text-fg-3">
      {chatTime(at)}
    </time>
  );
}

function UserMessage({ message, details }: { message: Message; details: boolean }) {
  const { t } = useTranslation();
  const { agentById, openTask } = useShell();
  const trigger: string = message.meta.trigger ?? "chat";
  const inbox: string[] = message.meta.inbox ?? [];
  const source = agentById(message.meta.source_agent_id);

  const briefing: string[] = message.meta.briefing_update ?? [];
  const briefingNote = details && briefing.length > 0 && (
    <Note icon={<RefreshCw />} label={t("chat.briefingUpdated", { sections: briefing.map((k) => t(`chat.section.${k}`, k)).join(", ") })} at={message.created_at} />
  );
  const updates = inbox.length > 0 && (
    <Note
      icon={<Bell />}
      label={inbox.length === 1 && !details ? firstLine(inbox[0]) : t("chat.updates", { count: inbox.length })}
      at={message.created_at}
      chevron={details}
      defaultOpen={details && trigger === "wake"}
    >
      <ul className="list-disc space-y-1 pl-4">
        {inbox.map((item, i) => (
          <li key={i} className="break-words">
            {item}
          </li>
        ))}
      </ul>
    </Note>
  );

  if (trigger === "chat") {
    return (
      <>
        {briefingNote}
        {updates}
        <div className={clsx(CHAT_ROW, "bg-sunk")}>
          <Head name={t("chat.you")} at={message.created_at} />
          {message.meta.attachments?.length > 0 && <MessageFiles files={message.meta.attachments} />}
          {message.content && <UserText text={message.content} />}
        </div>
      </>
    );
  }
  if (trigger === "wake")
    return (
      <>
        {briefingNote}
        {updates}
      </>
    );

  const cards: Record<string, { icon: ReactNode; title: string }> = {
    ticket: { icon: <Ticket />, title: t("chat.ticketHandoff") },
    assignment: { icon: <ClipboardList />, title: t("chat.assignmentFrom", { name: source?.name ?? t("chat.user") }) },
    ask: { icon: <HelpCircle />, title: t("chat.questionFrom", { name: source?.name ?? "?" }) },
    message: { icon: <Mail />, title: t("chat.messageFrom", { name: source?.name ?? "?" }) },
    schedule: { icon: <CalendarClock />, title: t("chat.scheduled") },
  };
  const card = cards[trigger] ?? { icon: <Mail />, title: trigger };
  return (
    <>
      {briefingNote}
      {updates}
      <Note
        icon={card.icon}
        label={card.title}
        at={message.created_at}
        chevron={details}
        action={
          message.meta.task_id ? (
            <button type="button" className={LINK} onClick={() => openTask(message.meta.task_id)}>
              {t("chat.openTicket")}
            </button>
          ) : undefined
        }
      >
        <Markdown>{message.content}</Markdown>
      </Note>
    </>
  );
}

function firstLine(text: string): string {
  const line = text.split(/\r?\n/)[0];
  return line.length > 110 ? `${line.slice(0, 109)}…` : line;
}

/** An event as one muted row: icon, label, time. Click to read what was sent. */
function Note({
  icon,
  label,
  at,
  children,
  action,
  chevron = false,
  defaultOpen = false,
}: {
  icon: ReactNode;
  label: string;
  at?: string;
  children?: ReactNode;
  action?: ReactNode;
  chevron?: boolean;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border-b border-line">
      <div className="flex items-center gap-2 px-4 py-2 text-fg-2">
        <button type="button" onClick={() => children && setOpen(!open)} aria-expanded={children ? open : undefined} className="flex min-w-0 flex-1 items-center gap-2 text-left hover:text-fg">
          {chevron && <ChevronRight className={clsx("size-4 shrink-0 text-fg-3 transition-transform", open && "rotate-90")} />}
          <span className="shrink-0 text-fg-3 [&>svg]:size-4">{icon}</span>
          <span className="truncate">{label}</span>
        </button>
        {action}
        {at && <MessageTime at={at} />}
      </div>
      {open && children && <div className="border-t border-line bg-sunk px-4 py-2.5 break-words text-fg-2">{children}</div>}
    </div>
  );
}

function MessageFiles({ files }: { files: { id: number; filename: string; content_type: string }[] }) {
  const [viewing, setViewing] = useState<number | null>(null);
  const images = files.filter((f) => f.content_type.startsWith("image/"));
  const others = files.filter((f) => !f.content_type.startsWith("image/"));
  return (
    <div className="my-1 flex flex-wrap items-start gap-1.5">
      {images.map((f, i) => (
        <button key={f.id} type="button" onClick={() => setViewing(i)} className="overflow-hidden rounded-sm ring-1 ring-line">
          <img src={`/api/attachments/${f.id}`} alt={f.filename} loading="lazy" className="h-40 max-w-full object-cover" />
        </button>
      ))}
      {others.map((f) => (
        <AttachmentLink key={f.id} file={f} download className="flex max-w-full items-center gap-1.5 rounded-sm px-2 py-1 ring-1 ring-line-2 ring-inset hover:bg-sunk">
          <Paperclip className="size-4 shrink-0 text-fg-3" />
          <span className="truncate">{f.filename}</span>
        </AttachmentLink>
      ))}
      {viewing !== null && <Lightbox images={images.map((f) => ({ src: `/api/attachments/${f.id}`, alt: f.filename }))} start={viewing} onClose={() => setViewing(null)} />}
    </div>
  );
}

const COLLAPSE_AT = 1200;

function UserText({ text }: { text: string }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const long = text.length > COLLAPSE_AT;
  return (
    <div className="break-words whitespace-pre-wrap">
      <TicketText text={long && !expanded ? `${text.slice(0, COLLAPSE_AT)}…` : text} linkClassName="underline decoration-line-2 underline-offset-[3px]" />
      {long && (
        <button type="button" onClick={() => setExpanded(!expanded)} className={clsx(LINK, "mt-1 block text-fg-2")}>
          {expanded ? t("chat.showLess") : t("chat.showMore", { count: text.length })}
        </button>
      )}
    </div>
  );
}

function toolLabel(name: string): string {
  return name.replace(/^mcp__plainboard__/, "").replace(/^mcp__([^_]+)__/, "$1 · ");
}

function ToolLine({ message }: { message: Message }) {
  const [open, setOpen] = useState(false);
  const { name, input, result, is_error } = message.meta as { name?: string; input?: string; result?: string; is_error?: boolean };
  const pending = result === undefined;
  return (
    <div className="border-b border-line">
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} className="flex w-full items-center gap-2 px-4 py-1.5 text-left text-fg-3 hover:bg-sunk">
        <Wrench className={clsx("size-4 shrink-0", is_error ? "text-fg" : pending && "animate-pulse text-fg")} />
        <span className={clsx("shrink-0 font-mono text-[0.9em]", is_error ? "font-semibold text-fg line-through decoration-fg-3" : "text-fg-2")}>{toolLabel(name ?? message.content)}</span>
        {input && input !== "{}" && <span className="truncate">{input}</span>}
      </button>
      {open && (
        <div className="space-y-1.5 border-t border-line bg-sunk px-4 py-2.5 font-mono text-[0.9em]">
          {input && <pre className="break-words whitespace-pre-wrap text-fg-2">{input}</pre>}
          {result !== undefined && <pre className={clsx("max-h-64 overflow-auto break-words whitespace-pre-wrap", is_error ? "text-fg-2" : "text-fg")}>{result || "(empty)"}</pre>}
        </div>
      )}
    </div>
  );
}

function SummaryDivider({ message }: { message: Message }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const label = message.meta.fresh ? "chat.freshContext" : message.meta.provider ? "chat.compactedByProvider" : "chat.compressed";
  return (
    <div className="border-b-2 border-fg">
      <button type="button" onClick={() => setOpen(!open)} disabled={!message.content} aria-expanded={open} className="flex w-full items-center gap-2 px-4 py-2 text-left text-fg-2 enabled:hover:text-fg">
        <Archive className="size-4 shrink-0 text-fg-3" />
        {t(label, { tokens: formatTokens(message.meta.tokens_before ?? 0) })}
      </button>
      {open && (
        <div className="border-t border-line bg-sunk px-4 py-2.5">
          <Markdown>{message.content}</Markdown>
        </div>
      )}
    </div>
  );
}
