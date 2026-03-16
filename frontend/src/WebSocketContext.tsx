import { createContext, useContext, useEffect, useRef, useState, useCallback, type ReactNode } from 'react';
import type { WSEvent } from './types';
import { getWsConfig } from './agentRegistry';

type Subscriber = (event: WSEvent) => void;

interface WSContextValue {
  connected: boolean;
  /** Whether the WebSocket has completed first-frame authentication */
  authenticated: boolean;
  subscribe: (callback: Subscriber) => () => void;
  /** Request replay of missed events for a project since a given sequence */
  requestReplay: (projectId: string, sinceSequence: number) => void;
}

const WSContext = createContext<WSContextValue>({
  connected: false,
  authenticated: false,
  subscribe: () => () => {},
  requestReplay: () => {},
});

// ── Auth token helpers ─────────────────────────────────────────────

/** LocalStorage key for the WebSocket / API auth token */
const AUTH_TOKEN_KEY = 'hivemind-auth-token';

/**
 * Retrieve the auth token used for the WebSocket first-frame auth protocol.
 *
 * Priority:
 *  1. localStorage (set via Settings or login flow)
 *  2. <meta name="hivemind-auth-token"> injected by backend into index.html
 *  3. empty string (backend may accept unauthenticated during migration)
 */
function getAuthToken(): string {
  try {
    const stored = localStorage.getItem(AUTH_TOKEN_KEY);
    if (stored) return stored;
  } catch {
    // localStorage unavailable (private browsing, etc.)
  }

  const meta = document.querySelector<HTMLMetaElement>('meta[name="hivemind-auth-token"]');
  if (meta?.content) return meta.content;

  return '';
}

/**
 * Persist an auth token (called from Settings or login flow).
 */
export function setAuthToken(token: string): void {
  try {
    if (token) {
      localStorage.setItem(AUTH_TOKEN_KEY, token);
    } else {
      localStorage.removeItem(AUTH_TOKEN_KEY);
    }
  } catch {
    // localStorage unavailable
  }
}

/**
 * Per-project sequence tracker.
 * Tracks the latest sequence_id seen for each project so we can request
 * only missed events on reconnect.
 */
const _projectSequences: Record<string, number> = {};

function _trackSequence(event: WSEvent) {
  if (event.project_id && typeof event.sequence_id === 'number') {
    const current = _projectSequences[event.project_id] ?? 0;
    if (event.sequence_id > current) {
      _projectSequences[event.project_id] = event.sequence_id;
    }
  }
}

/**
 * After a WebSocket reconnect, fetch live state for ALL projects
 * and request event replay for any missed events.
 *
 * Key fix: replay is requested for ALL projects with tracked sequences,
 * not just running ones. This ensures events emitted during a brief
 * disconnect (e.g., project just started while WS was down) are recovered.
 */
async function _syncStateOnReconnect(
  subscribers: Set<Subscriber>,
  ws: WebSocket | null,
) {
  try {
    const res = await fetch('/api/projects');
    if (!res.ok) return;
    const { projects } = await res.json();

    for (const project of projects ?? []) {
      if (!project.project_id) continue;

      // Dispatch a project_status event so dashboards refresh
      const statusEvent: WSEvent = {
        type: 'project_status',
        project_id: project.project_id,
        project_name: project.project_name,
        status: project.status ?? 'idle',
        timestamp: Date.now() / 1000,
      };
      for (const cb of subscribers) {
        try { cb(statusEvent); } catch { /* subscriber error */ }
      }

      // For running projects, fetch detailed live state
      if (project.is_running) {
        try {
          const liveRes = await fetch(`/api/projects/${project.project_id}/live`);
          if (liveRes.ok) {
            const liveData = await liveRes.json();
            const liveEvent: WSEvent = {
              type: 'live_state_sync',
              project_id: project.project_id,
              ...liveData,
              timestamp: Date.now() / 1000,
            };
            for (const cb of subscribers) {
              try { cb(liveEvent); } catch { /* subscriber error */ }
            }
          }
        } catch {
          // Ignore per-project fetch errors
        }
      }

      // Request event replay for ALL projects with tracked sequences
      // (not just running ones — events may have been emitted during disconnect)
      const lastSeq = _projectSequences[project.project_id] ?? 0;
      if (lastSeq > 0 && ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: 'replay',
          project_id: project.project_id,
          since_sequence: lastSeq,
        }));
      }
    }
  } catch {
    // Network error during sync — will retry on next reconnect
  }
}

export function WebSocketProvider({ children }: { children: ReactNode }) {
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const subscribersRef = useRef<Set<Subscriber>>(new Set());
  const wsConfig = getWsConfig();
  const retryDelayRef = useRef(wsConfig.reconnect_base_delay_ms);
  const mountedRef = useRef(true);
  const wasConnectedRef = useRef(false);
  const keepaliveRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;

    // Clear any pending reconnect timer
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }

    // Clear any existing keepalive interval
    if (keepaliveRef.current) {
      clearInterval(keepaliveRef.current);
      keepaliveRef.current = null;
    }

    // Don't create a new connection if one is already open/connecting
    const existing = wsRef.current;
    if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) {
      return;
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    const ws = new WebSocket(`${protocol}//${host}/ws`);

    ws.onopen = () => {
      setConnected(true);
      setAuthenticated(false); // not yet authenticated — waiting for auth_ok
      wasConnectedRef.current = true;
      retryDelayRef.current = wsConfig.reconnect_base_delay_ms; // reset backoff on success

      // ── First-frame authentication (SEC-WS) ──
      // Send auth token as the very first WebSocket message instead of
      // passing it as a query parameter (which leaks in server logs and
      // browser history).  The backend validates this frame and responds
      // with { type: "auth_ok" } or { type: "auth_error" }.
      const token = getAuthToken();
      try {
        ws.send(JSON.stringify({ type: 'auth', device_token: token }));
      } catch {
        // Send failed — will be caught by onclose
      }

      // Start client-side keepalive ping every 10 seconds.
      // iOS Safari aggressively kills idle WebSocket connections (~6-30s).
      // The server heartbeat (30s) is too slow — by the time it sends a ping,
      // iOS has already closed the connection. This client-side ping keeps
      // the connection alive by sending traffic at a shorter interval.
      keepaliveRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          try {
            ws.send(JSON.stringify({ type: 'pong' }));
          } catch {
            // Send failed — connection is dead, will be caught by onclose
          }
        }
      }, wsConfig.keepalive_interval_ms);

      // Note: state sync happens after auth_ok (not here, to avoid 401 floods)
    };

    ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        // Handle ping/pong at transport level — don't dispatch to subscribers
        if (data.type === 'ping') {
          ws.send(JSON.stringify({ type: 'pong' }));
          return;
        }

        // ── First-frame auth responses ──
        if (data.type === 'auth_ok') {
          setAuthenticated(true);
          // Now that we're authenticated, sync state
          _syncStateOnReconnect(subscribersRef.current, ws);
          return;
        }
        if (data.type === 'auth_failed') {
          setAuthenticated(false);
          // Stop reconnecting — redirect to login
          mountedRef.current = false;
          ws.close();
          window.dispatchEvent(new CustomEvent('hivemind-auth-expired'));
          return;
        }

        // Handle replay batch — dispatch each replayed event to subscribers
        if (data.type === 'replay_batch') {
          const events = data.events ?? [];
          for (const evt of events) {
            if (!evt || typeof evt !== 'object' || !evt.type) continue;
            const event = evt as WSEvent;
            _trackSequence(event);
            for (const cb of subscribersRef.current) {
              try { cb(event); } catch { /* subscriber error */ }
            }
          }
          return;
        }

        // Validate minimum required fields before dispatching
        if (!data.type || typeof data !== 'object') return;

        const event = data as WSEvent;
        _trackSequence(event);
        for (const cb of subscribersRef.current) {
          try {
            cb(event);
          } catch {
            // subscriber error — don't break others
          }
        }
      } catch {
        // ignore malformed messages
      }
    };

    ws.onclose = () => {
      setConnected(false);
      setAuthenticated(false);
      // Stop keepalive for this dead connection
      if (keepaliveRef.current) {
        clearInterval(keepaliveRef.current);
        keepaliveRef.current = null;
      }
      if (!mountedRef.current) return;
      // Exponential backoff with jitter: 1s → 2s → 4s → 8s → 16s → 30s cap
      // Jitter prevents thundering herd when server restarts (STAB-02)
      const baseDelay = retryDelayRef.current;
      const jitter = baseDelay * (0.5 + Math.random() * 0.5); // 50-100% of base
      retryDelayRef.current = Math.min(baseDelay * 2, wsConfig.reconnect_max_delay_ms);
      reconnectTimerRef.current = setTimeout(connect, jitter);
    };

    ws.onerror = () => {
      ws.close();
    };

    wsRef.current = ws;
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    connect();

    // ── Visibility change handler (critical for iOS Safari) ──
    // When the user switches back to the browser tab or returns from
    // background on mobile, iOS may have already killed the WebSocket.
    // We detect this and force an immediate reconnect instead of waiting
    // for the exponential backoff timer.
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible' && mountedRef.current) {
        const ws = wsRef.current;
        if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
          // Connection is dead — reconnect immediately (reset backoff)
          retryDelayRef.current = wsConfig.reconnect_base_delay_ms;
          connect();
        } else if (ws.readyState === WebSocket.OPEN) {
          // Connection is alive — but we may have missed events while in background.
          // Send a keepalive and re-sync state.
          try {
            ws.send(JSON.stringify({ type: 'pong' }));
          } catch {
            // Send failed — force reconnect
            ws.close();
          }
          _syncStateOnReconnect(subscribersRef.current, ws);
        }
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);

    // ── Page focus handler (backup for visibility change) ──
    // Some mobile browsers fire 'focus' but not 'visibilitychange' in
    // certain edge cases (e.g., switching between Safari tabs).
    const handleFocus = () => {
      if (mountedRef.current) {
        const ws = wsRef.current;
        if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
          retryDelayRef.current = wsConfig.reconnect_base_delay_ms;
          connect();
        }
      }
    };

    window.addEventListener('focus', handleFocus);

    // ── Online/offline handler ──
    // When the device regains network connectivity, reconnect immediately.
    const handleOnline = () => {
      if (mountedRef.current) {
        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) {
          retryDelayRef.current = wsConfig.reconnect_base_delay_ms;
          connect();
        }
      }
    };

    window.addEventListener('online', handleOnline);

    return () => {
      mountedRef.current = false;
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      window.removeEventListener('focus', handleFocus);
      window.removeEventListener('online', handleOnline);
      if (keepaliveRef.current) {
        clearInterval(keepaliveRef.current);
        keepaliveRef.current = null;
      }
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      wsRef.current?.close();
    };
  }, [connect]);

  const subscribe = useCallback((callback: Subscriber) => {
    subscribersRef.current.add(callback);
    return () => {
      subscribersRef.current.delete(callback);
    };
  }, []);

  const requestReplay = useCallback((projectId: string, sinceSequence: number) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'replay',
        project_id: projectId,
        since_sequence: sinceSequence,
      }));
    }
  }, []);

  return (
    <WSContext.Provider value={{ connected, authenticated, subscribe, requestReplay }}>
      {children}
    </WSContext.Provider>
  );
}

/**
 * Subscribe to WebSocket events. The callback is called for every event.
 * Returns { connected, authenticated, requestReplay } for sync control.
 */
export function useWSSubscribe(callback: Subscriber): {
  connected: boolean;
  authenticated: boolean;
  requestReplay: (projectId: string, sinceSequence: number) => void;
} {
  const { connected, authenticated, subscribe, requestReplay } = useContext(WSContext);
  const callbackRef = useRef(callback);
  callbackRef.current = callback;

  useEffect(() => {
    return subscribe((event) => callbackRef.current(event));
  }, [subscribe]);

  return { connected, authenticated, requestReplay };
}
