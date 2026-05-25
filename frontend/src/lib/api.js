import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API = `${BACKEND_URL}/api`;

const client = axios.create({ baseURL: API, timeout: 15000, withCredentials: true });
// Recovery operations need a longer timeout because they wait for on-chain
// confirmation (sell tx + getSignatureStatuses polling can take 25–40s).
const longClient = axios.create({ baseURL: API, timeout: 60000, withCredentials: true });

export const api = {
  wallet: () => client.get("/wallet").then(r => r.data),
  status: () => client.get("/bot/status").then(r => r.data),
  config: () => client.get("/bot/config").then(r => r.data),
  updateConfig: (cfg) => client.put("/bot/config", cfg).then(r => r.data),
  start: () => client.post("/bot/start").then(r => r.data),
  stop: () => client.post("/bot/stop").then(r => r.data),
  abortBot: () => client.post("/bot/abort").then(r => r.data),
  resetKillSwitch: () => client.post("/bot/reset-kill-switch").then(r => r.data),
  rules: () => client.get("/classifier/rules").then(r => r.data),
  updateRules: (rules) => client.put("/classifier/rules", rules).then(r => r.data),
  launches: (limit = 30) => client.get(`/launches/recent?limit=${limit}`).then(r => r.data),
  activeTrades: () => client.get("/trades/active").then(r => r.data),
  tradeHistory: (limit = 100) => client.get(`/trades/history?limit=${limit}`).then(r => r.data),
  exitTrade: (id) => client.post(`/trades/${id}/exit`).then(r => r.data),
  plSummary: (days = 7) => client.get(`/pl/summary?days=${days}`).then(r => r.data),
  plBySource: (days = 7) => client.get(`/pl/by-source?days=${days}`).then(r => r.data),
  insights: () => client.get(`/bot/insights`).then(r => r.data),
  sendSol: (to, amount_sol) => client.post("/wallet/send", { to, amount_sol }).then(r => r.data),
  reentryWatchlist: () => client.get("/reentry/watchlist").then(r => r.data),
  removeReentry: (mint) => client.delete(`/reentry/watchlist/${mint}`).then(r => r.data),
  scannerCandidates: () => client.get("/scanner/candidates").then(r => r.data),
  paperReset: () => client.post("/paper/reset").then(r => r.data),
  resetLivePnl: () => client.post("/pnl/reset-live").then(r => r.data),
  resetConfig: () => client.post("/bot/reset-config").then(r => r.data),
  suggestions: () => client.get("/suggestions").then(r => r.data),
  applySuggestion: (field, suggested) => client.post("/suggestions/apply", { field, suggested }).then(r => r.data),
  // Auth
  authMe: () => client.get("/auth/me").then(r => r.data),
  authSession: (sessionId) => client.post("/auth/session", null, { headers: { "X-Session-ID": sessionId } }).then(r => r.data),
  authLogout: () => client.post("/auth/logout").then(r => r.data),
  // Stuck-trade recovery
  stuckTrades: () => client.get("/trades/stuck").then(r => r.data),
  recoverStuck: (tradeId) => longClient.post(`/trades/recover/${tradeId}`).then(r => r.data),
  recoverStuckBatch: (tradeIds) => longClient.post("/trades/recover-batch", { trade_ids: tradeIds }, { timeout: 60000 + 30000 * tradeIds.length }).then(r => r.data),
  recoverStuckAll: () => longClient.post("/trades/recover-all", null, { timeout: 600000 }).then(r => r.data),
  // Wallet-wide token scan (finds ALL pump.fun tokens, even ones not in DB)
  walletTokenScan: () => longClient.get("/wallet/token-scan").then(r => r.data),
  walletRecoverMints: (mints) => longClient.post("/wallet/recover-mints", { mints }, { timeout: 60000 + 30000 * mints.length }).then(r => r.data),
  walletUnwrapWsol: () => longClient.post("/wallet/unwrap-wsol").then(r => r.data),
  // Strategy Doctor
  doctorList: (status = "pending") => client.get("/doctor/suggestions", { params: { status } }).then(r => r.data),
  doctorRunNow: () => longClient.post("/doctor/run-now").then(r => r.data),
  doctorApply: (id) => client.post(`/doctor/suggestions/${id}/apply`).then(r => r.data),
  doctorDismiss: (id) => client.post(`/doctor/suggestions/${id}/dismiss`).then(r => r.data),
  // Config sync (preview ↔ production)
  configExport: () => client.get("/config/export").then(r => r.data),
  configImport: (config) => client.post("/config/import", { config }).then(r => r.data),
  configApplyRecommended: () => client.post("/config/apply-recommended").then(r => r.data),
  recipientHealth: () => client.get("/diagnostics/recipient-health").then(r => r.data),
};
