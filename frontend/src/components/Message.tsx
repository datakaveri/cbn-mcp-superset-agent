import { LOGO_URL } from '../config';
import { PHASES } from '../types';
import type { AssistantMessage, ChatMessage, LogLevel } from '../types';
import { DashboardEmbed } from './DashboardEmbed';

const LEVEL_ICON: Record<LogLevel, string> = {
  info: '•',
  success: '✓',
  warning: '!',
  error: '✕',
};

function PhaseStrip({ phases }: { phases: AssistantMessage['phases'] }) {
  return (
    <div className="phase-strip">
      {PHASES.map(({ key, label }) => {
        const status = phases[key] ?? 'pending';
        return (
          <span key={key} className={`phase-chip ${status}`} title={`${label}: ${status}`}>
            {label}
          </span>
        );
      })}
    </div>
  );
}

function LogList({ logs }: { logs: AssistantMessage['logs'] }) {
  if (!logs.length) return null;
  return (
    <details className="log-block">
      <summary>{logs.length} log line{logs.length === 1 ? '' : 's'}</summary>
      <div className="log-list">
        {logs.map((l, i) => (
          <div key={i} className={`log-line ${l.level}`}>
            <span className="log-ts">{l.ts}</span>
            <span className="log-icon">{LEVEL_ICON[l.level]}</span>
            <span className="log-msg">{l.message}</span>
          </div>
        ))}
      </div>
    </details>
  );
}

function AssistantBubble({
  m,
  isLast,
  onSuggest,
}: {
  m: AssistantMessage;
  isLast: boolean;
  onSuggest: (q: string) => void;
}) {
  let status: string;
  if (m.running) status = 'Working…';
  else if (m.error) status = '⚠ ' + m.error;
  else if (m.success) status = '✅ Dashboard ready';
  else status = '⚠ Finished with issues';

  const meta = [
    m.chartCount != null ? `${m.chartCount} chart${m.chartCount === 1 ? '' : 's'}` : '',
    m.elapsed != null ? `${m.elapsed}s` : '',
  ]
    .filter(Boolean)
    .join(' · ');

  // Only the latest message embeds the live dashboard (avoids many heavy iframes);
  // older ones keep their status + logs.
  const showEmbed = isLast && !m.running && m.success && m.dashboardUuid;
  const showFollowups = isLast && !m.running && m.success && !!m.followups?.length;

  return (
    <div className="msg assistant">
      <div className="avatar" aria-hidden>
        <img src={LOGO_URL} alt="" />
      </div>
      <div className="bubble">
        <div className={`status-line ${m.running ? 'running' : m.success ? 'ok' : 'warn'}`}>
          {m.running && <span className="spinner" />}
          <span>{status}</span>
          {meta && <span className="meta">{meta}</span>}
        </div>
        <PhaseStrip phases={m.phases} />
        <LogList logs={m.logs} />
        {showEmbed && (
          <div className="embed-card">
            <DashboardEmbed key={`${m.dashboardUuid}:${m.chartCount}`} uuid={m.dashboardUuid!} />
          </div>
        )}
        {showFollowups && (
          <div className="followups">
            <span className="followups-label">Try next:</span>
            {m.followups!.map((q) => (
              <button key={q} className="ex-chip" onClick={() => onSuggest(q)}>
                {q}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export function Message({
  m,
  isLast,
  onSuggest,
}: {
  m: ChatMessage;
  isLast: boolean;
  onSuggest: (q: string) => void;
}) {
  if (m.role === 'user') {
    return (
      <div className="msg user">
        <div className="bubble">{m.text}</div>
      </div>
    );
  }
  return <AssistantBubble m={m} isLast={isLast} onSuggest={onSuggest} />;
}
