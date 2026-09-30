import axios from "axios";
import { installLeaderRouting } from "../api";
import { podRoute } from "../podRoute";

const bounce = (pod) => ({ status: 421, statusText: "Misdirected", headers: { "x-pod-retry": "1", "x-pod-id": pod, "x-pod-role": "follower" }, data: { detail: "follower" } });
const ok = (relayed) => ({ status: 200, statusText: "OK", headers: relayed ? { "x-pod-relayed": "1", "x-pod-role": "follower" } : { "x-pod-role": "leader" }, data: { ok: true } });

function mk(script) {
  const seen = [];
  const ax = axios.create({
    adapter: async (cfg) => {
      seen.push({ prefer: cfg.headers["X-Prefer-Leader"] });
      const r = script[Math.min(seen.length - 1, script.length - 1)];
      const res = { ...r, config: cfg };
      if (r.status >= 400) { const e = new Error("x"); e.config = cfg; e.response = res; throw e; }
      return res;
    },
  });
  installLeaderRouting(ax);
  return { ax, seen };
}

test("re-rolls the load balancer on 421 until the leader answers", async () => {
  const { ax, seen } = mk([bounce("pod-a"), bounce("pod-b"), ok(false)]);
  const r = await ax.get("/x");
  expect(r.data.ok).toBe(true);
  expect(seen.map((s) => s.prefer)).toEqual(["1", "1", "1"]);
});

test("last attempt drops the header so the follower relays instead of bouncing forever", async () => {
  const { ax, seen } = mk([bounce("pod-a"), bounce("pod-b"), bounce("pod-a"), ok(true)]);
  const r = await ax.post("/y", { a: 1 });
  expect(r.data.ok).toBe(true);
  expect(seen.map((s) => s.prefer)).toEqual(["1", "1", "1", undefined]);
  expect(podRoute.stats().relayed).toBeGreaterThan(0);
});

test("pinned to one follower by the ingress → relay immediately and stop bouncing for a while", async () => {
  const { ax, seen } = mk([bounce("pod-same"), bounce("pod-same"), bounce("pod-same"), ok(true)]);
  await ax.get("/z");
  expect(seen.map((s) => s.prefer)).toEqual(["1", "1", "1", undefined]);
  expect(podRoute.stats().pinned).toBe(true);
  const second = mk([ok(true)]);
  await second.ax.get("/z2");
  expect(second.seen[0].prefer).toBeUndefined();   // affinity window: no bounce attempts at all
});
