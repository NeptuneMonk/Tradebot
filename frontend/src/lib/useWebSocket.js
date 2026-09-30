import { useEffect, useRef, useState, useCallback } from "react";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || "";
const WS_URL = BACKEND_URL.replace(/^http/, "ws") + "/api/ws";
const LEADER_HUNT_ATTEMPTS = 6;     // fast re-rolls of the load balancer before accepting a follower's mirror feed
const LEADER_BOUNCE_CODE = 4409;    // follower closed us on purpose: reconnect, don't back off
const REHUNT_MS = 30_000;           // while on a mirror feed, probe for the leader this often and swap in-place

/**
 * Hook for backend event push.
 * Returns { connected, role } and accepts an onEvent callback.
 * Sticky-to-leader: connects with ?leader_only=1 so a follower pod bounces us (4409) and we re-roll the LB. After
 * LEADER_HUNT_ATTEMPTS bounces we accept the follower's Mongo-mirrored feed and keep probing for the leader in the
 * background, swapping sockets with no gap when one lands.
 * Reconnects with exponential backoff; the timer + socket are torn down on unmount so a remounted Dashboard never
 * ends up with two sockets or a stale handler.
 */
export function useWebSocket(onEvent) {
  const [connected, setConnected] = useState(false);
  const [role, setRole] = useState(null);
  const wsRef = useRef(null);
  const probeRef = useRef(null);
  const retryRef = useRef(0);
  const huntRef = useRef(0);
  const timerRef = useRef(null);
  const rehuntRef = useRef(null);
  const aliveRef = useRef(true);
  const handlerRef = useRef(onEvent);
  const statsRef = useRef({ msgs: 0, bytes: 0, since: Date.now(), bounces: 0, swaps: 0 });

  useEffect(() => { handlerRef.current = onEvent; }, [onEvent]);

  const schedule = useCallback((fn, delay) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => { timerRef.current = null; if (aliveRef.current) fn(); }, delay);
  }, []);

  const attach = useCallback((ws) => {
    ws.onmessage = (msg) => {
      if (!aliveRef.current || wsRef.current !== ws) return;
      try {
        statsRef.current.msgs += 1;
        statsRef.current.bytes += typeof msg.data === "string" ? msg.data.length : 0;
        const data = JSON.parse(msg.data);
        if (data?.type === "pod") { setRole(data.data?.role || null); return; }
        handlerRef.current && handlerRef.current(data);
      } catch (e) { /* ignore */ }
    };
  }, []);

  const connect = useCallback(() => {
    if (!aliveRef.current) return;
    if (wsRef.current) { try { wsRef.current.close(); } catch {} wsRef.current = null; }
    const hunting = huntRef.current < LEADER_HUNT_ATTEMPTS;
    let ws;
    try {
      ws = new WebSocket(hunting ? `${WS_URL}?leader_only=1` : WS_URL);
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
    attach(ws);
    ws.onclose = (ev) => {
      if (!aliveRef.current || wsRef.current !== ws) return;
      wsRef.current = null;
      setConnected(false);
      setRole(null);
      if (ev?.code === LEADER_BOUNCE_CODE) {
        huntRef.current += 1;
        statsRef.current.bounces += 1;
        schedule(connect, 120 + Math.random() * 180);
        return;
      }
      huntRef.current = 0;
      const delay = Math.min(10000, 500 * Math.pow(2, retryRef.current));
      retryRef.current += 1;
      schedule(connect, delay);
    };
    ws.onerror = () => {
      try { ws.close(); } catch {}
    };
  }, [schedule, attach]);

  // On a mirror feed: open a second socket asking for the leader; if it sticks, swap it in and drop the mirror.
  const probeLeader = useCallback(() => {
    if (!aliveRef.current || role !== "follower" || probeRef.current) return;
    let probe;
    try { probe = new WebSocket(`${WS_URL}?leader_only=1`); } catch { return; }
    probeRef.current = probe;
    probe.onmessage = (msg) => {
      let data = null;
      try { data = JSON.parse(msg.data); } catch { return; }
      if (data?.type !== "pod" || probeRef.current !== probe) return;
      probeRef.current = null;
      if (data.data?.role !== "leader") { try { probe.close(); } catch {} return; }
      const old = wsRef.current;
      wsRef.current = probe;
      attach(probe);
      probe.onclose = (ev) => {
        if (!aliveRef.current || wsRef.current !== probe) return;
        wsRef.current = null;
        setConnected(false);
        setRole(null);
        huntRef.current = ev?.code === LEADER_BOUNCE_CODE ? huntRef.current + 1 : 0;
        schedule(connect, 300);
      };
      probe.onerror = () => { try { probe.close(); } catch {} };
      statsRef.current.swaps += 1;
      huntRef.current = 0;
      setRole("leader");
      setConnected(true);
      try { old && old.close(); } catch {}
    };
    probe.onclose = () => { if (probeRef.current === probe) probeRef.current = null; };
    probe.onerror = () => { try { probe.close(); } catch {} };
  }, [role, attach, schedule, connect]);

  useEffect(() => {
    if (rehuntRef.current) { clearInterval(rehuntRef.current); rehuntRef.current = null; }
    if (role !== "follower") return;
    rehuntRef.current = setInterval(probeLeader, REHUNT_MS);
    return () => { if (rehuntRef.current) { clearInterval(rehuntRef.current); rehuntRef.current = null; } };
  }, [role, probeLeader]);

  useEffect(() => {
    aliveRef.current = true;
    connect();
    return () => {
      aliveRef.current = false;
      if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; }
      const ws = wsRef.current;
      wsRef.current = null;
      try { ws && ws.close(); } catch {}
      const probe = probeRef.current;
      probeRef.current = null;
      try { probe && probe.close(); } catch {}
    };
  }, [connect]);

  return { connected, role, statsRef };
}
