import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { Activity } from "lucide-react";

// REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH
export default function AuthCallback() {
  const navigate = useNavigate();
  const hasProcessed = useRef(false);

  useEffect(() => {
    if (hasProcessed.current) return;
    hasProcessed.current = true;

    const hash = window.location.hash || "";
    const m = hash.match(/session_id=([^&]+)/);
    const sessionId = m ? decodeURIComponent(m[1]) : null;

    if (!sessionId) {
      navigate("/login", { replace: true });
      return;
    }

    (async () => {
      try {
        const { user } = await api.authSession(sessionId);
        // Strip fragment so refresh doesn't re-trigger
        window.history.replaceState(null, "", "/dashboard");
        navigate("/dashboard", { replace: true, state: { user } });
      } catch (err) {
        const status = err?.response?.status;
        if (status === 403) {
          navigate("/login?denied=1", { replace: true });
        } else {
          navigate("/login", { replace: true });
        }
      }
    })();
  }, [navigate]);

  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-50 flex items-center justify-center" data-testid="auth-callback">
      <div className="flex items-center gap-3 text-neutral-400 font-mono text-xs uppercase tracking-[0.2em]">
        <Activity className="w-4 h-4 text-blue-500 animate-pulse" />
        Verifying session…
      </div>
    </div>
  );
}
