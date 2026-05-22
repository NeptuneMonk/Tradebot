import "@/App.css";
import Dashboard from "@/components/Dashboard";
import { Toaster } from "@/components/ui/sonner";

function App() {
  return (
    <div className="App min-h-screen bg-neutral-950 text-neutral-50">
      <Dashboard />
      <Toaster theme="dark" />
    </div>
  );
}

export default App;
