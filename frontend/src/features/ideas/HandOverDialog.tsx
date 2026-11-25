import { useQueryClient } from "@tanstack/react-query";
import { Send } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { upsertIdea } from "../../api/events";
import type { Agent, Idea } from "../../api/types";
import { Button, Modal, Textarea } from "../../components/ui";
import { useDraft } from "../../lib/drafts";
import { useShell } from "../layout/shell-context";
import { ideaTitle } from "./IdeaCard";

/** Ideas to the manager (#163): it makes tickets of them, guided by an optional note. The server writes the chat
 * message ("Ideas handed over:", services/ideas.hand_over) and marks the ideas, so nobody hands them over twice. */
export function HandOverDialog({
  manager,
  ideas,
  all,
  onClose,
  onSent,
}: {
  manager: Agent;
  ideas: Idea[];
  /** From "all to the manager": the intro speaks of all open ideas. */
  all: boolean;
  onClose: () => void;
  onSent: () => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const pid = useShell().project.id;
  const [text, setText, clearText] = useDraft(`idea-handover:${pid}:${all ? "all" : ideas[0]?.id}`);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const area = useRef<HTMLTextAreaElement>(null);
  useEffect(() => area.current?.focus(), []);

  const send = async () => {
    if (sending) return;
    setSending(true);
    setError("");
    try {
      const out = await api.post<{ agent_id: number; ideas: Idea[] }>(`/projects/${pid}/ideas/hand-over`, { idea_ids: ideas.map((i) => i.id), message: text.trim() });
      out.ideas.forEach((idea) => upsertIdea(qc, pid, idea));
      clearText();
      onSent();
    } catch (err) {
      setError(errorMessage(err));
      setSending(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      width="max-w-lg"
      title={all ? t("ideas.handOver.titleAll", { name: manager.name }) : t("ideas.handOver.titleOne", { name: manager.name })}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button variant="primary" icon={<Send className="size-4" />} loading={sending} onClick={send}>
            {t("ideas.handOver.send", { name: manager.name })}
          </Button>
        </>
      }
    >
      <div data-part="idea-handover" className="flex flex-col gap-3">
        <p className="text-fg-2">
          {all ? t("ideas.handOver.introAll", { name: manager.name, count: ideas.length }) : t("ideas.handOver.introOne", { name: manager.name, title: ideaTitle(ideas[0]) })}
        </p>
        <Textarea
          ref={area}
          rows={3}
          value={text}
          maxLength={10000}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder={t("ideas.handOver.placeholder", { name: manager.name })}
          aria-label={t("ideas.handOver.placeholder", { name: manager.name })}
          className="max-h-[40vh]"
        />
        {error && <p className="text-you-fg">{error}</p>}
      </div>
    </Modal>
  );
}
