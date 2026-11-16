import clsx from "clsx";
import { Shield } from "lucide-react";
import { useTranslation } from "react-i18next";
import { AgentDot, AgentSquare } from "../../components/ui";
import { formatTokens } from "../../lib/format";
import { useIsDesktop, useMediaQuery } from "../../lib/hooks";
import { useShell } from "../layout/shell-context";
import { ChatAside } from "./ChatAside";
import { ChatPanel } from "./ChatPanel";

/** Full-page chat: agent list, large conversation and, where there is room, what surrounds it
 * (see ChatAside). On phones the agent switcher is in the header. */
export function ChatView({ agentId, onSelectAgent }: { agentId: number; onSelectAgent: (id: number) => void }) {
  const { t } = useTranslation();
  const { agents } = useShell();
  const isDesktop = useIsDesktop();
  const aside = useMediaQuery("(min-width: 1280px)");
  const agent = agents.find((a) => a.id === agentId);

  return (
    <div className="flex h-full">
      {isDesktop && (
        <aside className="flex w-56 shrink-0 flex-col border-r border-line 2xl:w-60">
          <h2 className="caps mx-2.5 flex h-12 shrink-0 items-center border-b-2 border-fg">{t("chat.conversations")}</h2>
          <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
            {agents.map((agent) => (
              <button
                key={agent.id}
                type="button"
                onClick={() => onSelectAgent(agent.id)}
                aria-current={agent.id === agentId ? "true" : undefined}
                title={[t(`agentStatus.${agent.status}`), ...(agent.status === "background" ? agent.job_commands : [])].join("\n")}
                className={clsx("grid w-full grid-cols-[9px_auto_minmax(0,1fr)] items-center gap-x-2 border-b border-line px-2.5 py-2 text-left", agent.id === agentId ? "bg-sunk" : "hover:bg-sunk")}
              >
                <AgentDot status={agent.status} />
                <AgentSquare agent={agent} />
                <span className="min-w-0">
                  <span className={clsx("flex items-center gap-1.5", agent.id === agentId ? "font-extrabold" : "font-medium")}>
                    <span className="truncate">{agent.name}</span>
                    {agent.is_manager && <Shield className="size-3.5 shrink-0 text-fg-3" aria-label={t("team.manager")} />}
                  </span>
                  <span className="block truncate text-fg-3">
                    {agent.role || "—"} · {formatTokens(agent.context_tokens)}
                  </span>
                </span>
              </button>
            ))}
          </div>
        </aside>
      )}
      <section className="flex min-w-0 flex-1 flex-col">
        <ChatPanel agentId={agentId} onSelectAgent={onSelectAgent} variant="page" />
      </section>
      {aside && agent && <ChatAside agent={agent} />}
    </div>
  );
}
