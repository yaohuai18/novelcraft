import { Navigate, NavLink, Route, Routes, useNavigate } from "react-router-dom";
import LoginPage from "./pages/LoginPage";
import ProjectsPage from "./pages/ProjectsPage";
import ProjectDetailPage from "./pages/ProjectDetailPage";
import ChapterEditorPage from "./pages/ChapterEditorPage";
import CharactersPage from "./pages/CharactersPage";
import RelationshipsPage from "./pages/RelationshipsPage";
import TimelinePage from "./pages/TimelinePage";
import SuggestionsPage from "./pages/SuggestionsPage";
import ForeshadowsPage from "./pages/ForeshadowsPage";
import ConsistencyPage from "./pages/ConsistencyPage";
import TasksPage from "./pages/TasksPage";
import ReviewCenterPage from "./pages/ReviewCenterPage";
import CollaborationPage from "./pages/CollaborationPage";
import AuditLogPage from "./pages/AuditLogPage";
import CreatePage from "./pages/CreatePage";
import PublishPage from "./pages/PublishPage";
import SettingsPage from "./pages/SettingsPage";
import { clearToken, getToken, getUser } from "./api/client";
import { useState } from "react";

function RequireAuth({ children }: { children: JSX.Element }) {
  if (!getToken()) {
    return <Navigate to="/login" replace />;
  }
  return children;
}

function Shell() {
  const navigate = useNavigate();
  const user = getUser();
  const [username] = useState(user?.username || "");
  const initial = (username || "我").slice(0, 1).toUpperCase();

  const navItems = [
    { to: "/projects", icon: "📚", label: "作品库" },
    { to: "/create", icon: "✍️", label: "创作" },
    { to: "/publish", icon: "🚀", label: "发布" },
    { to: "/review-center", icon: "🛡️", label: "审核中心" },
    { to: "/collaboration", icon: "👥", label: "协作权限" },
    { to: "/audit-logs", icon: "🧾", label: "操作审计" },
    { to: "/settings", icon: "⚙️", label: "设置" },
  ];

  function handleLogout() {
    clearToken();
    navigate("/login");
  }

  return (
    <div className="layout">
      <div className="sidebar">
        <div className="brand">
          <div className="brand-logo">🖋</div>
          <span>NovelCraft</span>
        </div>
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
          >
            <span className="nav-icon">{item.icon}</span>
            <span>{item.label}</span>
          </NavLink>
        ))}
        <div className="sidebar-footer">
          NovelCraft 创作平台 · v0.3
          <br />
          后端 API 位于 :8000，前端 :5173
        </div>
      </div>
      <div className="main">
        <div className="topbar">
          <div className="user-chip">
            <span>{username || "未登录"}</span>
            <div className="avatar">{initial}</div>
            <button className="secondary" onClick={handleLogout}>
              退出
            </button>
          </div>
        </div>
        <Routes>
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
          <Route path="/projects/:projectId/characters" element={<CharactersPage />} />
          <Route path="/projects/:projectId/relationships" element={<RelationshipsPage />} />
          <Route path="/projects/:projectId/timeline" element={<TimelinePage />} />
          <Route path="/projects/:projectId/suggestions" element={<SuggestionsPage />} />
<Route path="/projects/:projectId/foreshadows" element={<ForeshadowsPage />} />
          <Route path="/projects/:projectId/consistency" element={<ConsistencyPage />} />
          <Route path="/projects/:projectId/tasks" element={<TasksPage />} />
          <Route
            path="/projects/:projectId/chapters/:chapterId?"
            element={<ChapterEditorPage />}
          />
          <Route path="/create" element={<CreatePage />} />
          <Route path="/publish" element={<PublishPage />} />
          <Route path="/review-center" element={<ReviewCenterPage />} />
          <Route path="/collaboration" element={<CollaborationPage />} />
          <Route path="/audit-logs" element={<AuditLogPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/projects" replace />} />
        </Routes>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/*"
        element={
          <RequireAuth>
            <Shell />
          </RequireAuth>
        }
      />
    </Routes>
  );
}
