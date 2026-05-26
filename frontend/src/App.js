import "@/App.css";
import { useEffect } from "react";
import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router-dom";
import Dashboard from "@/components/Dashboard";
import Login from "@/components/Login";
import AuthCallback from "@/components/AuthCallback";
import ProtectedRoute from "@/components/ProtectedRoute";
import { Toaster } from "@/components/ui/sonner";

// REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH
function AppRouter() {
  const location = useLocation();
  // Synchronous race-condition guard: if returning from OAuth, jump to callback handler first.
  if (location.hash?.includes("session_id=")) {
    return <AuthCallback />;
  }
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        path="/dashboard"
        element={
          <ProtectedRoute>
            <Dashboard />
          </ProtectedRoute>
        }
      />
      <Route path="/" element={<Navigate to="/dashboard" replace />} />
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  );
}

function App() {
  // Periodic Performance buffer cleanup. React 19's profiling build calls
  // `performance.mark` / `performance.measure` on every render. On long
  // mobile sessions (especially with the high-churn WS dashboard) the
  // entries accumulate until Chrome runs out of memory on the structured
  // clone for DevTools, surfacing as:
  //   `DataCloneError: Failed to execute 'measure' on 'Performance':
  //    Data cannot be cloned, out of memory.`
  // Clearing every 30s keeps the buffer bounded without affecting React.
  useEffect(() => {
    const id = setInterval(() => {
      try {
        if (typeof performance !== "undefined") {
          if (performance.clearMarks) performance.clearMarks();
          if (performance.clearMeasures) performance.clearMeasures();
        }
      } catch { /* noop */ }
    }, 30_000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="App min-h-screen bg-neutral-950 text-neutral-50">
      <BrowserRouter>
        <AppRouter />
      </BrowserRouter>
      <Toaster theme="dark" />
    </div>
  );
}

export default App;
