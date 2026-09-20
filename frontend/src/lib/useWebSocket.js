import { useEffect, useRef, useState, useCallback } from "react";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || "";
const WS_URL = BACKEND_URL.replace(/^http/, "ws") + "/api/ws";

/**
 * Hook for backend event push.
 * Returns { connected } and accepts an onEvent callback.
 * Reconnects with exponential backoff; the timer + socket are torn down on unmount so a
 * remounted Dashboard never ends up with two sockets or a stale handler.
 */
export function useWebSocket(onEvent) {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef(null);
  const retryRef = useRef(0);
  const timerRef = useRef(null);
  const aliveRef = useRef(true);
  const handlerRef = useRef(onEvent);
  const statsRef = useRef({ msgs: 0, bytes: 0, since: Date.now() });

  useEffect(() => { handlerRef.current = onEvent; }, [onEvent]);

  const schedule = useCallback((fn, delay) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => { timerRef.current = null; if (aliveRef.current) fn(); }, delay);
  }, []);

  const connect = useCallback(() => {
    if (!aliveRef.current) return;
    if (wsRef.current) { try { wsRef.current.close(); } catch {} wsRef.current = null; }
    let ws;
    try {
      ws = new WebSocket(WS_URL);
    } catch (e) {
      schedule(connect, 1500);
      return;
    }
    wsRef.current = ws;
    ws.onopen = () => {
      if (!aliveRef.current || wsRef.current !== ws) return;
      setConnected(true);
      retryRef.current = 0;
    };
    ws.onmessage = (msg) => {
      if (!aliveRef.current || wsRef.current !== ws) return;
      try {
        statsRef.current.msgs += 1;
        statsRef.current.bytes += typeof msg.data === "string" ? msg.data.length : 0;
        const data = JSON.parse(msg.data);
        handlerRef.current && handlerRef.current(data);
      } catch (e) { /* ignore */ }
    };
    ws.onclose = () => {
      if (!aliveRef.current || wsRef.current !== ws) return;
      wsRef.current = null;
      setConnected(false);
      const delay = Math.min(10000, 500 * Math.pow(2, retryRef.current));
      retryRef.current += 1;
      schedule(connect, delay);
    };
    ws.onerror = () => {
      try { ws.close(); } catch {}
    };
  }, [schedule]);

  useEffect(() => {
    aliveRef.current = true;
    connect();
    return () => {
      aliveRef.current = false;
      if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; }
      const ws = wsRef.current;
      wsRef.current = null;
      try { ws && ws.close(); } catch {}
    };
  }, [connect]);

  return { connected, statsRef };
}
