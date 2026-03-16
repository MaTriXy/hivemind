import { useCallback, useEffect, useRef, useState } from 'react';
import { useWSSubscribe } from '../WebSocketContext';
import type { WSEvent } from '../types';
import { AGENT_LABELS } from '../constants';

// ─────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────

interface LogEntry {
  id: string;
  timestamp: number;
  agent?: string;
  type: WSEvent['type'];
  summary: string;
  detail?: string;
}

interface Props {
  projectId: string;
  /** Max log entries to keep before dropping oldest. Default: 200 */
  maxEntries?: number;
}

// ─────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────

/** Summarise a WSEvent into a human-readable one-liner. */
function summariseEvent(event: WSEvent): { summary: string; detail?: string } | null {
  switch (event.type) {
    case 'agent_started':
      return {
        summary: `${event.agent ?? 'Agent'} started`,
        detail: event.task ? event.task.slice(0, 120) : undefined,
      };
    case 'agent_finished':
      return {
        summary: `${event.agent ?? 'Agent'} ${event.is_error ? 'failed' : 'finished'}`,
        detail: event.failure_reason ?? undefined,
      };
    case 'tool_use':
      return {
        summary: `${event.agent ?? '?'} → ${event.tool_name ?? 'tool'}`,
        detail: event.description?.slice(0, 120),
      };
    case 'delegation':
      return {
        summary: `Delegated ${event.from_agent ?? '?'} → ${event.to_agent ?? '?'}`,
      };
    case 'agent_update':
      return event.progress
        ? { summary: `${event.agent ?? 'Agent'}: ${event.progress.slice(0, 100)}` }
        : null;
    case 'agent_result':
      return event.summary
        ? { summary: `Result: ${event.summary.slice(0, 100)}` }
        : null;
    case 'agent_final':
      return { summary: 'Task completed' };
    case 'project_status':
      return event.status ? { summary: `Status → ${event.status}` } : null;
    case 'loop_progress':
      return {
        summary: `Loop ${event.loop ?? '?'}/${event.max_loops ?? '?'}, turn ${event.turn ?? '?'}/${event.max_turns ?? '?'}`,
      };
    case 'self_healing':
      return {
        summary: `Self-healing: retrying ${event.failed_task ?? 'task'}`,
        detail: event.failure_category,
      };
    case 'task_complete':
      return { summary: `DAG task complete: ${event.task_id ?? ''}` };
    case 'task_error':
      return { summary: `DAG task error: ${event.task_id ?? ''}` };
    default:
      return null;
  }
}

/** Colour for the agent indicator dot. */
const AGENT_DOT_COLOURS: Record<string, string> = {
  developer:          'var(--accent-blue)',
  frontend_developer: 'var(--accent-blue)',
  backend_developer:  '#4f6ef5',
  reviewer:           'var(--accent-purple)',
  tester:             'var(--accent-amber)',
  test_engineer:      'var(--accent-amber)',
  devops:             'var(--accent-cyan)',
  researcher:         '#10b981',
  orchestrator:       'var(--text-muted)',
  security_auditor:   'var(--accent-red)',
  pm:                 '#8b5cf6',
};

function agentDotColor(agent: string | undefined): string {
  if (!agent) return 'var(--text-muted)';
  return AGENT_DOT_COLOURS[agent.toLowerCase()] ?? 'var(--accent-blue)';
}

/** Format a Unix seconds timestamp as HH:MM:SS */
function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}

// ─────────────────────────────────────────────────────────────
// Main component
// ─────────────────────────────────────────────────────────────

export default function AgentLogPanel({ projectId, maxEntries = 200 }: Props): React.ReactElement {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [collapsed, setCollapsed] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const counterRef = useRef(0);

  const handleEvent = useCallback((event: WSEvent) => {
    if (event.project_id !== projectId) return;

    const parsed = summariseEvent(event);
    if (!parsed) return;

    const entry: LogEntry = {
      id: `${projectId}-${++counterRef.current}`,
      timestamp: event.timestamp,
      agent: event.agent ?? (event.from_agent ? `${event.from_agent}→${event.to_agent}` : undefined),
      type: event.type,
      summary: parsed.summary,
      detail: parsed.detail,
    };

    setEntries(prev => {
      const next = [...prev, entry];
      return next.length > maxEntries ? next.slice(next.length - maxEntries) : next;
    });
  }, [projectId, maxEntries]);

  useWSSubscribe(handleEvent);

  // Auto-scroll to bottom when new entries arrive
  useEffect(() => {
    if (!autoScroll || collapsed) return;
    const el = scrollRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }, [entries, autoScroll, collapsed]);

  // Detect manual scroll away from bottom → disable auto-scroll
  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setAutoScroll(distFromBottom < 40);
  }, []);

  const entryCount = entries.length;

  return (
    <section
      aria-label={`Agent log for project ${projectId}`}
      style={{
        background: 'var(--bg-panel)',
        border: '1px solid var(--border-dim)',
        borderRadius: '12px',
        overflow: 'hidden',
      }}
    >
      {/* ── Header bar ── */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          padding: '8px 12px',
          borderBottom: collapsed ? 'none' : '1px solid var(--border-dim)',
          background: 'var(--bg-elevated)',
        }}
      >
        {/* Toggle button */}
        <button
          type="button"
          onClick={() => setCollapsed(c => !c)}
          aria-expanded={!collapsed}
          aria-controls={`log-body-${projectId}`}
          aria-label={collapsed ? 'Expand agent log' : 'Collapse agent log'}
          className="focus-ring"
          style={{
            background: 'none',
            border: 'none',
            cursor: 'pointer',
            padding: '2px 4px',
            borderRadius: '4px',
            color: 'var(--text-muted)',
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
            outline: 'none',
          }}
          onFocus={e => { e.currentTarget.style.outline = '2px solid var(--focus-ring)'; e.currentTarget.style.outlineOffset = '2px'; }}
          onBlur={e => { e.currentTarget.style.outline = 'none'; }}
        >
          {/* Chevron */}
          <svg
            width="12"
            height="12"
            viewBox="0 0 12 12"
            fill="none"
            aria-hidden="true"
            style={{
              transform: collapsed ? 'rotate(-90deg)' : 'rotate(0deg)',
              transition: 'transform 0.2s ease',
              flexShrink: 0,
            }}
          >
            <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>

        {/* Live indicator + label */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flex: 1, minWidth: 0 }}>
          <span
            aria-hidden="true"
            style={{
              width: '6px',
              height: '6px',
              borderRadius: '50%',
              background: entryCount > 0 ? 'var(--accent-green)' : 'var(--text-muted)',
              flexShrink: 0,
              animation: entryCount > 0 ? 'pulse 2s ease-in-out infinite' : 'none',
            }}
          />
          <span
            style={{
              fontSize: '11px',
              fontWeight: 600,
              fontFamily: 'var(--font-mono)',
              color: 'var(--text-secondary)',
              letterSpacing: '0.05em',
              textTransform: 'uppercase',
            }}
          >
            Agent Log
          </span>
          {entryCount > 0 && (
            <span
              style={{
                fontSize: '10px',
                fontFamily: 'var(--font-mono)',
                color: 'var(--text-muted)',
                background: 'var(--bg-card)',
                padding: '1px 6px',
                borderRadius: '4px',
                border: '1px solid var(--border-dim)',
              }}
            >
              {entryCount}
            </span>
          )}
        </div>

        {/* Clear button */}
        {entryCount > 0 && (
          <button
            type="button"
            onClick={() => setEntries([])}
            aria-label="Clear agent log"
            style={{
              background: 'none',
              border: 'none',
              cursor: 'pointer',
              padding: '2px 6px',
              borderRadius: '4px',
              fontSize: '10px',
              fontFamily: 'var(--font-mono)',
              color: 'var(--text-muted)',
              outline: 'none',
              flexShrink: 0,
            }}
            onFocus={e => { e.currentTarget.style.outline = '2px solid var(--focus-ring)'; e.currentTarget.style.outlineOffset = '2px'; }}
            onBlur={e => { e.currentTarget.style.outline = 'none'; }}
            onMouseEnter={e => { e.currentTarget.style.color = 'var(--text-primary)'; e.currentTarget.style.background = 'var(--bg-card)'; }}
            onMouseLeave={e => { e.currentTarget.style.color = 'var(--text-muted)'; e.currentTarget.style.background = 'none'; }}
          >
            clear
          </button>
        )}

        {/* Auto-scroll toggle */}
        {!collapsed && entryCount > 3 && (
          <button
            type="button"
            onClick={() => setAutoScroll(a => !a)}
            aria-pressed={autoScroll}
            aria-label={autoScroll ? 'Disable auto-scroll' : 'Enable auto-scroll'}
            style={{
              background: autoScroll ? 'var(--glow-green)' : 'none',
              border: `1px solid ${autoScroll ? 'rgba(61,214,140,0.2)' : 'var(--border-dim)'}`,
              cursor: 'pointer',
              padding: '2px 6px',
              borderRadius: '4px',
              fontSize: '10px',
              fontFamily: 'var(--font-mono)',
              color: autoScroll ? 'var(--accent-green)' : 'var(--text-muted)',
              outline: 'none',
              flexShrink: 0,
            }}
            onFocus={e => { e.currentTarget.style.outline = '2px solid var(--focus-ring)'; e.currentTarget.style.outlineOffset = '2px'; }}
            onBlur={e => { e.currentTarget.style.outline = 'none'; }}
          >
            ↓ auto
          </button>
        )}
      </div>

      {/* ── Log body ── */}
      {!collapsed && (
        <div
          id={`log-body-${projectId}`}
          ref={scrollRef}
          onScroll={handleScroll}
          role="log"
          aria-live="polite"
          aria-label="Streaming agent events"
          style={{
            height: '180px',
            overflowY: 'auto',
            padding: '8px 0',
          }}
        >
          {entryCount === 0 ? (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                height: '100%',
                fontSize: '12px',
                color: 'var(--text-muted)',
                fontFamily: 'var(--font-mono)',
                fontStyle: 'italic',
              }}
            >
              Waiting for agent events…
            </div>
          ) : (
            entries.map(entry => (
              <div
                key={entry.id}
                style={{
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: '8px',
                  padding: '4px 12px',
                  animation: 'messageFadeIn 0.2s ease-out',
                }}
              >
                {/* Agent colour dot */}
                <span
                  aria-hidden="true"
                  style={{
                    width: '6px',
                    height: '6px',
                    borderRadius: '50%',
                    background: agentDotColor(entry.agent),
                    flexShrink: 0,
                    marginTop: '5px',
                  }}
                />

                {/* Timestamp */}
                <span
                  aria-label={`Time: ${formatTime(entry.timestamp)}`}
                  style={{
                    fontSize: '10px',
                    fontFamily: 'var(--font-mono)',
                    color: 'var(--text-muted)',
                    flexShrink: 0,
                    marginTop: '2px',
                    userSelect: 'none',
                  }}
                >
                  {formatTime(entry.timestamp)}
                </span>

                {/* Agent name (if present) */}
                {entry.agent && (
                  <span
                    style={{
                      fontSize: '10px',
                      fontFamily: 'var(--font-mono)',
                      color: agentDotColor(entry.agent),
                      flexShrink: 0,
                      marginTop: '2px',
                      fontWeight: 600,
                      maxWidth: '80px',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {AGENT_LABELS[entry.agent] || entry.agent}
                  </span>
                )}

                {/* Summary + detail */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <p
                    style={{
                      fontSize: '11px',
                      color: 'var(--text-primary)',
                      margin: 0,
                      lineHeight: 1.4,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {entry.summary}
                  </p>
                  {entry.detail && (
                    <p
                      style={{
                        fontSize: '10px',
                        color: 'var(--text-muted)',
                        margin: 0,
                        lineHeight: 1.4,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        fontFamily: 'var(--font-mono)',
                      }}
                    >
                      {entry.detail}
                    </p>
                  )}
                </div>
              </div>
            ))
          )}
        </div>
      )}
    </section>
  );
}
