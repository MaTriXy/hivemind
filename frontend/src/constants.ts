/**
 * Shared agent constants — derived from the backend AGENT_REGISTRY.
 *
 * These exports maintain backward compatibility with existing components.
 * The actual data comes from agentRegistry.ts which fetches from /api/agent-registry.
 *
 * For new code, prefer importing directly from agentRegistry.ts:
 *   import { getAgentIcon, getAgentLabel, getAgentAccent } from './agentRegistry';
 */

import {
  getAgentIcons,
  getAgentLabels,
  getAgentAccent as _getAgentAccent,
  getAgentColors,
} from './agentRegistry';

// ── Backward-compatible static-like exports ───────────────────────
// These are getter-based so they always reflect the latest registry data.
// Components that import AGENT_ICONS['developer'] will still work.

/** Agent emoji icons — derived from AGENT_REGISTRY */
export const AGENT_ICONS: Record<string, string> = new Proxy(
  {} as Record<string, string>,
  {
    get(_target, prop: string) {
      return getAgentIcons()[prop] ?? '🤖';
    },
    ownKeys() {
      return Object.keys(getAgentIcons());
    },
    getOwnPropertyDescriptor(_target, prop: string) {
      const icons = getAgentIcons();
      if (prop in icons) {
        return { configurable: true, enumerable: true, value: icons[prop] };
      }
      return undefined;
    },
    has(_target, prop: string) {
      return prop in getAgentIcons();
    },
  }
);

/** Agent display labels — derived from AGENT_REGISTRY */
export const AGENT_LABELS: Record<string, string> = new Proxy(
  {} as Record<string, string>,
  {
    get(_target, prop: string) {
      return getAgentLabels()[prop] ?? prop;
    },
    ownKeys() {
      return Object.keys(getAgentLabels());
    },
    getOwnPropertyDescriptor(_target, prop: string) {
      const labels = getAgentLabels();
      if (prop in labels) {
        return { configurable: true, enumerable: true, value: labels[prop] };
      }
      return undefined;
    },
    has(_target, prop: string) {
      return prop in getAgentLabels();
    },
  }
);

/** Agent Tailwind color classes — derived from AGENT_REGISTRY */
export const AGENT_COLORS: Record<string, { border: string; bg: string; text: string }> = new Proxy(
  {} as Record<string, { border: string; bg: string; text: string }>,
  {
    get(_target, prop: string) {
      return getAgentColors(prop);
    },
  }
);

/** Accent colors for per-agent styling — derived from AGENT_REGISTRY */
export const AGENT_ACCENTS: Record<string, { color: string; glow: string; bg: string }> = new Proxy(
  {} as Record<string, { color: string; glow: string; bg: string }>,
  {
    get(_target, prop: string) {
      return _getAgentAccent(prop);
    },
  }
);

/** Get accent colors for an agent (with fallback) */
export function getAgentAccent(name: string) {
  return _getAgentAccent(name);
}

/**
 * Format a Unix timestamp to a time string.
 * @param ts Unix timestamp in seconds
 * @param showSeconds Whether to include seconds
 */
export function formatTime(ts: number, showSeconds = false): string {
  const opts: Intl.DateTimeFormatOptions = { hour: '2-digit', minute: '2-digit' };
  if (showSeconds) opts.second = '2-digit';
  return new Date(ts * 1000).toLocaleTimeString([], opts);
}
