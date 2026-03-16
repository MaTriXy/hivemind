import { useEffect } from 'react';

/**
 * Dynamically sets the page title. Resets to 'Hivemind' on unmount.
 * Usage: usePageTitle('Dashboard') => 'Hivemind — Dashboard'
 *        usePageTitle('My Project', 'running') => 'Hivemind — My Project ● Running'
 */
export function usePageTitle(subtitle?: string, status?: string) {
  useEffect(() => {
    const parts = ['Hivemind'];
    if (subtitle) parts.push(subtitle);
    if (status && status !== 'idle') {
      const statusLabel = status.charAt(0).toUpperCase() + status.slice(1);
      document.title = `${parts.join(' — ')} ● ${statusLabel}`;
    } else {
      document.title = parts.join(' — ');
    }
    return () => { document.title = 'Hivemind'; };
  }, [subtitle, status]);
}
