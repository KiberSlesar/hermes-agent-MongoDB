/**
 * FleetChatPanel — control-plane chat that follows messaging_owner.
 *
 * Uses GatewayClient → /api/fleet/ws (proxied to the active agent's serve).
 * Polls /api/fleet/active-chat and reconnects when owner / api_base changes.
 * Does not spawn a local PTY/agent loop on the Mongo box.
 */

import { Button } from "@nous-research/ui/ui/components/button";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Card } from "@nous-research/ui/ui/components/card";
import { RefreshCw, Send } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { GatewayClient, type ConnectionState } from "@/lib/gatewayClient";
import { api, type FleetActiveChat } from "@/lib/api";
import { cn } from "@/lib/utils";
import { usePageHeader } from "@/contexts/usePageHeader";

type ChatLine = {
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
};

const STATE_TONE: Record<
  ConnectionState,
  "secondary" | "warning" | "success" | "destructive"
> = {
  idle: "secondary",
  connecting: "warning",
  open: "success",
  closed: "secondary",
  error: "destructive",
};

function targetKey(t: FleetActiveChat | null): string {
  if (!t) return "";
  return `${t.owner_node_id ?? ""}|${t.api_base ?? ""}`;
}

export function FleetChatPanel({ isActive = true }: { isActive?: boolean }) {
  const { setTitle } = usePageHeader();
  const [target, setTarget] = useState<FleetActiveChat | null>(null);
  const [conn, setConn] = useState<ConnectionState>("idle");
  const [lines, setLines] = useState<ChatLine[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [epoch, setEpoch] = useState(0);

  const sessionIdRef = useRef<string | null>(null);
  const assistantBufRef = useRef("");
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const lastKeyRef = useRef("");

  useEffect(() => {
    if (!isActive) {
      setTitle(null);
      return;
    }
    const label = target?.hostname || target?.owner_node_id;
    setTitle(label ? `Fleet · ${label}` : "Fleet chat");
    return () => setTitle(null);
  }, [isActive, target, setTitle]);

  const refreshTarget = useCallback(async () => {
    try {
      const next = await api.getFleetActiveChat();
      setTarget(next);
      return next;
    } catch (e) {
      setError(`Fleet status failed: ${e}`);
      return null;
    }
  }, []);

  // Poll active-chat; bump epoch when owner/api_base flips so WS reconnects.
  useEffect(() => {
    if (!isActive) return;
    let cancelled = false;
    const tick = async () => {
      const next = await refreshTarget();
      if (cancelled || !next) return;
      const key = targetKey(next);
      if (lastKeyRef.current && lastKeyRef.current !== key) {
        setLines((prev) => [
          ...prev,
          {
            id: `sys-${Date.now()}`,
            role: "system",
            text: `Handoff: chat now follows ${next.hostname || next.owner_node_id || "new owner"}`,
          },
        ]);
        setEpoch((e) => e + 1);
      }
      lastKeyRef.current = key;
    };
    void tick();
    const id = window.setInterval(() => void tick(), 4000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [isActive, refreshTarget]);

  const gw = useMemo(() => new GatewayClient(), [epoch]);

  useEffect(() => {
    if (!isActive) return;
    let cancelled = false;
    sessionIdRef.current = null;
    assistantBufRef.current = "";
    setConn("connecting");
    setError(null);

    const offState = gw.onState(setConn);

    const unsubDelta = gw.on("message.delta", (ev) => {
      const text = String((ev.payload as { text?: string } | undefined)?.text ?? "");
      if (!text) return;
      assistantBufRef.current += text;
      const buf = assistantBufRef.current;
      setLines((prev) => {
        const last = prev[prev.length - 1];
        if (last?.role === "assistant" && last.id.startsWith("a-stream")) {
          return [...prev.slice(0, -1), { ...last, text: buf }];
        }
        return [...prev, { id: `a-stream-${Date.now()}`, role: "assistant", text: buf }];
      });
    });

    const unsubComplete = gw.on("message.complete", (ev) => {
      const text =
        String((ev.payload as { text?: string } | undefined)?.text ?? "") ||
        assistantBufRef.current;
      assistantBufRef.current = "";
      setBusy(false);
      if (!text) return;
      setLines((prev) => {
        const withoutStream = prev.filter((l) => !l.id.startsWith("a-stream"));
        return [...withoutStream, { id: `a-${Date.now()}`, role: "assistant", text }];
      });
    });

    const unsubErr = gw.on("error", (ev) => {
      const message = (ev.payload as { message?: string } | undefined)?.message;
      if (message) setError(message);
      setBusy(false);
    });

    (async () => {
      try {
        const t = await refreshTarget();
        if (cancelled) return;
        if (t && t.handoff_state && !["idle", "done", ""].includes(t.handoff_state)) {
          setError(`Handoff in progress (${t.handoff_state}) — waiting for chat_ready…`);
        }
        if (t && !t.chat_ready) {
          setError(
            t.api_base
              ? `Active agent not reachable at ${t.api_base}`
              : "Active agent has no api_base — set HERMES_API_BASE and run hermes serve",
          );
          setConn("error");
          return;
        }
        await gw.connect();
        if (cancelled) return;
        const created = await gw.request<{ session_id: string }>("session.create", {
          source: "dashboard",
          close_on_disconnect: true,
        });
        sessionIdRef.current = created.session_id;
        setError(null);
      } catch (e) {
        if (!cancelled) {
          setConn("error");
          setError(String(e));
        }
      }
    })();

    return () => {
      cancelled = true;
      offState();
      unsubDelta();
      unsubComplete();
      unsubErr();
      gw.close();
    };
  }, [gw, isActive, epoch, refreshTarget]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [lines]);

  const send = useCallback(async () => {
    const text = draft.trim();
    if (!text || busy || !sessionIdRef.current) return;
    setDraft("");
    setBusy(true);
    assistantBufRef.current = "";
    setLines((prev) => [...prev, { id: `u-${Date.now()}`, role: "user", text }]);
    try {
      await gw.request("prompt.submit", {
        session_id: sessionIdRef.current,
        text,
      });
    } catch (e) {
      setBusy(false);
      setError(String(e));
    }
  }, [busy, draft, gw]);

  const reconnect = () => {
    setError(null);
    setEpoch((e) => e + 1);
  };

  const handoff =
    !!target?.handoff_state && !["idle", "done", ""].includes(target.handoff_state);

  return (
    <div className="flex h-full min-h-0 w-full flex-col gap-3 p-3">
      <Card className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2 text-sm">
          <Badge tone={STATE_TONE[conn]}>{conn}</Badge>
          <span className="text-muted-foreground">
            {target?.hostname || target?.owner_node_id || "no owner"}
            {target?.api_base ? ` · ${target.api_base}` : ""}
          </span>
          {handoff ? (
            <Badge tone="warning">handoff: {target?.handoff_state}</Badge>
          ) : null}
          {target?.chat_ready === false ? (
            <Badge tone="destructive">chat not ready</Badge>
          ) : null}
        </div>
        <Button size="sm" ghost onClick={reconnect} prefix={<RefreshCw className="h-3.5 w-3.5" />}>
          Reconnect
        </Button>
      </Card>

      {(error || handoff) && (
        <div
          className={cn(
            "rounded-md border px-3 py-2 text-sm",
            error ? "border-destructive/40 text-destructive" : "border-warning/40 text-warning",
          )}
        >
          {error ||
            `Messaging handoff (${target?.handoff_state}) — chat will reconnect when ready.`}
        </div>
      )}

      <div
        ref={scrollRef}
        className="min-h-0 flex-1 overflow-y-auto rounded-md border border-border bg-background/40 px-3 py-3"
      >
        {lines.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Chat runs on the active home agent via control-plane proxy. Activate a
            node on System when you need to switch machines.
          </p>
        ) : (
          <ul className="flex flex-col gap-3">
            {lines.map((line) => (
              <li
                key={line.id}
                className={cn(
                  "whitespace-pre-wrap text-sm",
                  line.role === "user" && "text-foreground",
                  line.role === "assistant" && "text-foreground/90",
                  line.role === "system" && "text-muted-foreground italic",
                )}
              >
                <span className="mr-2 text-xs uppercase tracking-wide text-muted-foreground">
                  {line.role}
                </span>
                {line.text}
              </li>
            ))}
          </ul>
        )}
      </div>

      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <input
          className="min-w-0 flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-1 focus:ring-ring"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={conn === "open" ? "Message the active agent…" : "Waiting for connection…"}
          disabled={conn !== "open" || busy}
        />
        <Button
          type="submit"
          size="sm"
          disabled={conn !== "open" || busy || !draft.trim()}
          prefix={<Send className="h-3.5 w-3.5" />}
        >
          Send
        </Button>
      </form>
    </div>
  );
}
