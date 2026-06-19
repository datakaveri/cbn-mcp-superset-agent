import { useCallback, useRef, useState } from 'react';
import { AuthError, runPipeline } from '../api';
import { relogin } from '../auth/keycloak';
import type { AssistantMessage, ChatMessage } from '../types';

let counter = 0;
const newId = () => `m${++counter}`;
const ts = () => new Date().toLocaleTimeString('en-GB', { hour12: false });

/**
 * Owns the chat transcript and drives a pipeline run. Each query appends a user
 * message and an assistant message; the assistant message is mutated live as
 * SSE events arrive (phase status, log lines), then finalized with the result.
 */
export function usePipeline() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [running, setRunning] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const patch = useCallback(
    (id: string, fn: (m: AssistantMessage) => AssistantMessage) => {
      setMessages((ms) =>
        ms.map((m) => (m.id === id && m.role === 'assistant' ? fn(m) : m)),
      );
    },
    [],
  );

  const run = useCallback(
    async (query: string) => {
      const q = query.trim();
      if (!q || running) return;

      const aid = newId();
      setMessages((ms) => [
        ...ms,
        { id: newId(), role: 'user', text: q },
        { id: aid, role: 'assistant', query: q, logs: [], phases: {}, running: true },
      ]);
      setRunning(true);

      const ac = new AbortController();
      abortRef.current = ac;
      const start = performance.now();

      try {
        for await (const ev of runPipeline(q, ac.signal)) {
          patch(aid, (m) => {
            const next: AssistantMessage = {
              ...m,
              phases: { ...m.phases },
              logs: m.logs,
            };
            if (ev.phase && ev.level) {
              const k = ev.phase.toLowerCase();
              if (ev.level === 'success') next.phases[k] = 'done';
              else if (ev.level === 'error') next.phases[k] = 'error';
              else if (!next.phases[k] || next.phases[k] === 'pending')
                next.phases[k] = 'running';
            }
            if (ev.message) {
              next.logs = [
                ...m.logs,
                { ts: ts(), phase: ev.phase, level: ev.level ?? 'info', message: ev.message },
              ];
            }
            if (ev.done) {
              next.running = false;
              next.success = ev.success;
              next.dashboardUuid = ev.dashboard_uuid || undefined;
              next.dashboardUrl = ev.dashboard_url || undefined;
              next.chartCount = ev.charts;
              next.elapsed = Math.round((performance.now() - start) / 100) / 10;
            }
            return next;
          });
        }
      } catch (e) {
        if (e instanceof AuthError) relogin();
        patch(aid, (m) => ({
          ...m,
          running: false,
          success: false,
          error: e instanceof Error ? e.message : String(e),
        }));
      } finally {
        setRunning(false);
        abortRef.current = null;
      }
    },
    [running, patch],
  );

  return { messages, running, run };
}
