import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { api, User } from "./api/client";
import { AuthPage } from "./pages/AuthPage";
import { Dashboard } from "./pages/Dashboard";
import "./styles/app.css";

function App() {
  const [user, setUser] = useState<User | null>(null);
  const [mode, setMode] = useState<"login" | "register">("login");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function boot() {
      try {
        const [bootstrap] = await Promise.all([api.bootstrap()]);
        setMode(bootstrap.has_users ? "login" : "register");
        const me = await api.me().catch(() => null);
        setUser(me);
      } finally {
        setLoading(false);
      }
    }
    boot();
  }, []);

  async function logout() {
    await api.logout();
    setUser(null);
    setMode("login");
  }

  if (loading) {
    return <div className="loading">加载中...</div>;
  }

  if (!user) {
    return <AuthPage mode={mode} onAuthed={setUser} />;
  }

  return <Dashboard user={user} onLogout={logout} />;
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
