// Sticky-to-leader routing state shared by the axios client and the WebSocket hook.
// Every /api call first asks for the leader pod directly (X-Prefer-Leader). A follower answers 421 in ~1 ms and we
// re-roll the load balancer; only the last attempt falls back to the follower's Mongo relay. If the same follower
// keeps answering, the ingress is pinning us to it (cookie affinity) — stop bouncing for a while and just relay.

export const HTTP_MAX_ATTEMPTS = 4;
export const AFFINITY_TTL_MS = 30_000;

const state = { affinityUntil: 0, lastPodId: null, samePodStreak: 0, stats: { bounced: 0, relayed: 0, affinity: 0 } };

export const podRoute = {
  preferLeader: () => Date.now() >= state.affinityUntil,
  /** Record a 421 bounce from `podId`. Returns true when the client should give up bouncing (pinned to one follower). */
  noteBounce(podId) {
    state.stats.bounced += 1;
    state.samePodStreak = podId && podId === state.lastPodId ? state.samePodStreak + 1 : 0;
    state.lastPodId = podId || null;
    if (state.samePodStreak >= 2) {
      state.affinityUntil = Date.now() + AFFINITY_TTL_MS;
      state.stats.affinity += 1;
      state.samePodStreak = 0;
      return true;
    }
    return false;
  },
  noteRelayed: () => { state.stats.relayed += 1; },
  noteLeader: () => { state.samePodStreak = 0; },
  stats: () => ({ ...state.stats, pinned: Date.now() < state.affinityUntil }),
};
