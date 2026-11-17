import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, setToken, setUser } from "../api/client";

export default function LoginPage() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (mode === "register") {
        if (!email) {
          setError("注册需要填写邮箱");
          setBusy(false);
          return;
        }
        await api.register({ username, email, password });
        // 注册成功后自动登录
      }
      const tokenRes = await api.login({ username, password });
      setToken(tokenRes.access_token);
      const me = await api
        .listProjects()
        .then(() => ({ username }))
        .catch(() => ({ username }));
      setUser(me);
      navigate("/projects");
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败，请重试");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-wrap">
      <div className="card auth-card">
        <div className="brand">
          <div className="brand-logo">🖋</div>
          <span>NovelCraft 创作平台</span>
        </div>
        <p style={{ textAlign: "center", color: "var(--text-dim)", fontSize: 13, margin: "0 0 18px" }}>
          登录后管理你的作品、章节与创作进度
        </p>
        <div className="auth-tabs">
          <div
            className={`auth-tab ${mode === "login" ? "active" : ""}`}
            onClick={() => setMode("login")}
          >
            登录
          </div>
          <div
            className={`auth-tab ${mode === "register" ? "active" : ""}`}
            onClick={() => setMode("register")}
          >
            注册
          </div>
        </div>
        {error && <div className="error-box">{error}</div>}
        <form onSubmit={handleSubmit}>
          <div className="field">
            <label>用户名</label>
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="3-50 个字符"
              required
            />
          </div>
          {mode === "register" && (
            <div className="field">
              <label>邮箱</label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                required
              />
            </div>
          )}
          <div className="field">
            <label>密码</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="至少 6 位"
              required
            />
          </div>
          <button type="submit" style={{ width: "100%" }} disabled={busy}>
            {busy ? "请稍候…" : mode === "login" ? "登 录" : "注册并登录"}
          </button>
        </form>
      </div>
    </div>
  );
}
