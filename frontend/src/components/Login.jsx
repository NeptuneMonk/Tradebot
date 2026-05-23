import { useState } from "react";
import { Activity, ShieldCheck, AlertTriangle } from "lucide-react";
import { useLocation } from "react-router-dom";

// REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH
export default function Login() {
  const location = useLocation();
  const denied = new URLSearchParams(location.search).get("denied") === "1";
  const [redirecting, setRedirecting] = useState(false);

  const handleSignIn = () => {
    setRedirecting(true);
    // REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH
    const redirectUrl = window.location.origin + "/dashboard";
    window.location.href = `https://auth.emergentagent.com/?redirect=${encodeURIComponent(redirectUrl)}`;
  };

  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-50 flex items-center justify-center px-4" data-testid="login-page">
      <div className="absolute inset-0 pointer-events-none opacity-[0.04]"
           style={{ backgroundImage: "radial-gradient(circle at 20% 10%, #3b82f6 0px, transparent 40%), radial-gradient(circle at 80% 90%, #ef4444 0px, transparent 35%)" }} />

      <div className="relative w-full max-w-md">
        <div className="flex items-center gap-3 mb-8 justify-center">
          <Activity className="w-6 h-6 text-blue-500" />
          <div>
            <h1 className="text-base font-mono font-bold tracking-tight" data-testid="login-title">PUMP.BOT // micro-stake</h1>
            <p className="text-[10px] uppercase tracking-[0.2em] text-neutral-500">authorized access only</p>
          </div>
        </div>

        <div className="border border-neutral-800 bg-neutral-900/60 backdrop-blur-sm rounded-md p-6 shadow-2xl">
          <div className="flex items-center gap-2 text-xs font-mono text-neutral-400 uppercase tracking-wider mb-4">
            <ShieldCheck className="w-3.5 h-3.5 text-emerald-500" />
            <span>single-user lockdown</span>
          </div>

          <h2 className="text-lg font-mono font-semibold mb-2">Sign in to continue</h2>
          <p className="text-xs text-neutral-400 leading-relaxed mb-6">
            Wallet, trades, and config are gated behind your Google account.
            Only the whitelisted email may proceed — no other device, account, or
            session can view or control this bot.
          </p>

          {denied && (
            <div
              className="mb-4 flex items-start gap-2 border border-red-900/60 bg-red-950/40 text-red-200 text-xs font-mono rounded-md p-3"
              data-testid="login-denied-banner"
            >
              <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
              <div>
                <div className="font-bold uppercase tracking-wider mb-1">Access denied</div>
                <div className="text-red-300/80">
                  This Google account is not on the allow-list. Sign in with the
                  authorized email or contact the owner.
                </div>
              </div>
            </div>
          )}

          <button
            type="button"
            onClick={handleSignIn}
            disabled={redirecting}
            data-testid="login-google-btn"
            className="w-full flex items-center justify-center gap-3 bg-white text-neutral-900 font-mono text-sm font-semibold rounded-md py-2.5 px-4 hover:bg-neutral-100 active:scale-[0.99] transition disabled:opacity-60"
          >
            <svg className="w-4 h-4" viewBox="0 0 24 24" aria-hidden="true">
              <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
              <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.99.66-2.26 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
              <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
              <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84C6.71 7.31 9.14 5.38 12 5.38z"/>
            </svg>
            <span>{redirecting ? "Redirecting…" : "Sign in with Google"}</span>
          </button>

          <div className="mt-6 text-[10px] text-neutral-600 font-mono leading-relaxed text-center">
            Session lasts 1 hour. You'll be asked to sign in again after expiry.
          </div>
        </div>

        <div className="mt-6 text-[10px] text-neutral-600 font-mono text-center uppercase tracking-[0.2em]">
          preview-only · solana mainnet · real funds
        </div>
      </div>
    </div>
  );
}
