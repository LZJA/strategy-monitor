import { FormEvent, useState } from "react";
import { api, User } from "../api/client";

type Props = {
  mode: "login" | "register";
  onAuthed: (user: User) => void;
};

export function AuthPage({ mode, onAuthed }: Props) {
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const user =
        mode === "register"
          ? await api.register(username, password, email || undefined)
          : await api.login(username, password);
      onAuthed(user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败");
    }
  }

  return (
    <main className="auth-shell">
      <form className="auth-panel" onSubmit={submit}>
        <div>
          <p className="eyebrow">Strategy Monitor</p>
          <h1>{mode === "register" ? "创建账号" : "登录"}</h1>
        </div>
        <label>
          用户名
          <input value={username} onChange={(event) => setUsername(event.target.value)} required />
        </label>
        {mode === "register" && (
          <label>
            邮箱
            <input value={email} onChange={(event) => setEmail(event.target.value)} type="email" />
          </label>
        )}
        <label>
          密码
          <input
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            type="password"
            minLength={8}
            required
          />
        </label>
        {error && <p className="error">{error}</p>}
        <button type="submit">{mode === "register" ? "注册并进入" : "登录"}</button>
      </form>
    </main>
  );
}
