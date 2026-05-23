import { useEffect, useState } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { api } from "@/lib/api";
import { Activity } from "lucide-react";

export default function ProtectedRoute({ children }) {
  const location = useLocation();
  // If AuthCallback just passed user via state, skip the round-trip
  const [isAuthed, setIsAuthed] = useState(location.state?.user ? true : null);
  const [user, setUser] = useState(location.state?.user || null);

  useEffect(() => {
    if (isAuthed === true) return;
    // Race-condition guard: if URL has session_id, let AuthCallback handle it.
    if (window.location.hash?.includes("session_id=")) return;
    let cancelled = false;
    (async () => {
      try {
        const u = await api.authMe();
        if (!cancelled) {
          setUser(u);
          setIsAuthed(true);
        }
      } catch {
        if (!cancelled) setIsAuthed(false);
      }
    })();
    return () => { cancelled = true; };
  }, [isAuthed]);

  if (isAuthed === null) {
    return (
      <div className="min-h-screen bg-neutral-950 text-neutral-50 flex items-center justify-center" data-testid="auth-checking">
        <div className="flex items-center gap-3 text-neutral-400 font-mono text-xs uppercase tracking-[0.2em]">
          <Activity className="w-4 h-4 text-blue-500 animate-pulse" />
          Checking session…
        </div>
      </div>
    );
  }

  if (!isAuthed) return <Navigate to="/login" replace />;

  // Inject `user` prop into children if it's a single element
  return typeof children === "function" ? children(user) : children;
}
