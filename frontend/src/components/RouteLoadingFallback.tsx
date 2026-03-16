/**
 * RouteLoadingFallback — Suspense fallback for lazy-loaded route chunks.
 *
 * Shows a minimal, branded loading indicator that matches the app's visual
 * language.  Uses a short delay (200ms) before showing the spinner so fast
 * loads feel instant (no flash-of-loading-state).
 */

import { useEffect, useState, type ReactElement } from 'react';

/** Delay in ms before the spinner becomes visible (avoids flash on fast loads) */
const SHOW_DELAY_MS = 200;

export default function RouteLoadingFallback(): ReactElement {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => setVisible(true), SHOW_DELAY_MS);
    return () => clearTimeout(timer);
  }, []);

  if (!visible) {
    // Render an empty container that matches the page background so there's
    // no layout shift or flash of white while we wait for the chunk.
    return (
      <div
        className="flex-1 h-full"
        style={{ background: 'var(--bg-void)' }}
        aria-hidden="true"
      />
    );
  }

  return (
    <div
      className="flex items-center justify-center h-full min-h-[50vh]"
      style={{ background: 'var(--bg-void)' }}
      role="status"
      aria-label="Loading page"
    >
      <div className="flex flex-col items-center gap-3">
        {/* Animated pulsing dots */}
        <div className="flex items-center gap-1.5" aria-hidden="true">
          {[0, 1, 2].map((i) => (
            <span
              key={i}
              className="block w-2 h-2 rounded-full"
              style={{
                background: 'var(--accent-blue, #638cff)',
                opacity: 0.4,
                animation: `routePulse 1.2s ease-in-out ${i * 0.15}s infinite`,
              }}
            />
          ))}
        </div>
        <span
          className="text-xs font-medium"
          style={{ color: 'var(--text-muted, #8b90a5)' }}
        >
          Loading…
        </span>
      </div>

      {/* Inline keyframes — scoped to this component, no external CSS needed */}
      <style>{`
        @keyframes routePulse {
          0%, 80%, 100% { opacity: 0.3; transform: scale(0.8); }
          40% { opacity: 1; transform: scale(1.2); }
        }
      `}</style>
    </div>
  );
}
