import { useEffect, useRef, useState, useCallback } from "react";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || "";
const WS_URL = BACKEND_URL.replace(/^http/, "ws") + "/api/ws";

/**
 * Hook for backend event push.
 * Returns { connected, lastEvent } and accepts an onEvent callback.
 */
export function useWebSocket(onEvent) {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef(null);
  const retryRef = useRef(0);
  const handlerRef = useRef(onEvent);

  useEffect(() => { handlerRef.current = onEvent; }, [onEvent]);

  const connect = useCallback(() => {
    try {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;
      ws.onopen = () => {
        setConnected(true);
        retryRef.current = 0;
      };
      ws.onmessage = (msg) => {
        try {
          const data = JSON.parse(msg.data);
          handlerRef.current && handlerRef.current(data);
        } catch (e) { /* ignore */ }
      };
      ws.onclose = () => {
        setConnected(false);
        // Exponential backoff up to 10s
        const delay = Math.min(10000, 500 * Math.pow(2, retryRef.current));
        retryRef.current += 1;
        setTimeout(connect, delay);
      };
      ws.onerror = () => {
        try { ws.close(); } catch {}
      };
    } catch (e) {
      setTimeout(connect, 1500);
    }
  }, []);

  useEffect(() => {
    connect();
    return () => {
      try { wsRef.current && wsRef.current.close(); } catch {}
    };
  }, [connect]);

  return { connected };
}
