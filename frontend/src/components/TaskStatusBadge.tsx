import type { ReactElement } from 'react';

/** Maps DAG task status strings to display config */
type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'failed' | string;

interface Props {
  status: TaskStatus;
  /** If true, renders a more compact version (no text label) */
  compact?: boolean;
  /** Override aria-label for the badge */
  label?: string;
}

interface StatusConfig {
  color: string;
  bg: string;
  border: string;
  dot: boolean;
  pulse: boolean;
  text: string;
}

const STATUS_MAP: Record<string, StatusConfig> = {
  pending: {
    color: 'var(--text-muted)',
    bg: 'rgba(139,144,165,0.1)',
    border: 'rgba(139,144,165,0.2)',
    dot: false,
    pulse: false,
    text: 'Pending',
  },
  in_progress: {
    color: 'var(--accent-blue)',
    bg: 'rgba(99,140,255,0.1)',
    border: 'rgba(99,140,255,0.2)',
    dot: true,
    pulse: true,
    text: 'In Progress',
  },
  running: {
    color: 'var(--accent-green)',
    bg: 'rgba(61,214,140,0.1)',
    border: 'rgba(61,214,140,0.2)',
    dot: true,
    pulse: true,
    text: 'Running',
  },
  completed: {
    color: 'var(--accent-green)',
    bg: 'rgba(61,214,140,0.08)',
    border: 'rgba(61,214,140,0.15)',
    dot: false,
    pulse: false,
    text: 'Completed',
  },
  done: {
    color: 'var(--accent-green)',
    bg: 'rgba(61,214,140,0.08)',
    border: 'rgba(61,214,140,0.15)',
    dot: false,
    pulse: false,
    text: 'Done',
  },
  failed: {
    color: 'var(--accent-red)',
    bg: 'rgba(245,71,91,0.08)',
    border: 'rgba(245,71,91,0.15)',
    dot: false,
    pulse: false,
    text: 'Failed',
  },
  error: {
    color: 'var(--accent-red)',
    bg: 'rgba(245,71,91,0.08)',
    border: 'rgba(245,71,91,0.15)',
    dot: false,
    pulse: false,
    text: 'Error',
  },
  paused: {
    color: 'var(--accent-amber)',
    bg: 'rgba(245,166,35,0.1)',
    border: 'rgba(245,166,35,0.2)',
    dot: false,
    pulse: false,
    text: 'Paused',
  },
  idle: {
    color: 'var(--text-muted)',
    bg: 'rgba(139,144,165,0.08)',
    border: 'rgba(139,144,165,0.12)',
    dot: false,
    pulse: false,
    text: 'Idle',
  },
  stopped: {
    color: 'var(--accent-red)',
    bg: 'rgba(245,71,91,0.08)',
    border: 'rgba(245,71,91,0.12)',
    dot: false,
    pulse: false,
    text: 'Stopped',
  },
};

function getConfig(status: string): StatusConfig {
  return STATUS_MAP[status.toLowerCase()] ?? {
    color: 'var(--text-muted)',
    bg: 'rgba(139,144,165,0.08)',
    border: 'rgba(139,144,165,0.1)',
    dot: false,
    pulse: false,
    text: status.charAt(0).toUpperCase() + status.slice(1),
  };
}

/**
 * A compact, accessible status badge for task/agent states.
 * Supports: pending | in_progress | running | completed | done |
 *           failed | error | paused | idle | stopped + any unknown value.
 */
export default function TaskStatusBadge({ status, compact = false, label }: Props): ReactElement {
  const cfg = getConfig(status);

  const ariaLabel = label ?? `Status: ${cfg.text}`;

  return (
    <span
      role="status"
      aria-label={ariaLabel}
      className="status-badge-pop"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '4px',
        padding: compact ? '1px 6px' : '2px 8px',
        borderRadius: '9999px',
        background: cfg.bg,
        border: `1px solid ${cfg.border}`,
        flexShrink: 0,
      }}
    >
      {/* Indicator dot */}
      <span
        aria-hidden="true"
        style={{
          width: compact ? '5px' : '6px',
          height: compact ? '5px' : '6px',
          borderRadius: '50%',
          background: cfg.dot || !compact ? cfg.color : 'transparent',
          flexShrink: 0,
          animation: cfg.pulse ? 'pulse 1.5s ease-in-out infinite' : 'none',
          opacity: cfg.dot ? 1 : 0.6,
        }}
      />
      {/* Label */}
      {!compact && (
        <span
          aria-hidden="true"
          style={{
            fontSize: '10px',
            fontWeight: 700,
            letterSpacing: '0.06em',
            fontFamily: 'var(--font-mono)',
            color: cfg.color,
            textTransform: 'uppercase',
            whiteSpace: 'nowrap',
          }}
        >
          {cfg.text}
        </span>
      )}
    </span>
  );
}
