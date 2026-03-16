import { useEffect, useRef } from 'react';
import { useWSSubscribe } from '../WebSocketContext';
import { useToast } from '../components/Toast';

/**
 * Fires a toast notification when the WebSocket connection state changes.
 *
 * - Disconnect → error toast within 500 ms (actually immediate, <100 ms)
 * - Reconnect → success toast
 *
 * Must be mounted inside both ToastProvider and WebSocketProvider.
 */
export function useWSConnectionToast(): void {
  const { connected } = useWSSubscribe(() => {});
  const { error, success } = useToast();
  // null = not yet mounted (skip first render)
  const prevRef = useRef<boolean | null>(null);
  const toastFiredRef = useRef(false);

  useEffect(() => {
    // Skip the very first mount; record initial state only
    if (prevRef.current === null) {
      prevRef.current = connected;
      return;
    }

    if (!connected && prevRef.current) {
      // Transition: connected → disconnected
      toastFiredRef.current = true;
      error('Connection lost', 'Reconnecting to server — live updates paused.');
    } else if (connected && !prevRef.current) {
      // Transition: disconnected → connected
      if (toastFiredRef.current) {
        success('Connection restored', 'Live data stream is active again.');
        toastFiredRef.current = false;
      }
    }

    prevRef.current = connected;
  }, [connected, error, success]);
}
