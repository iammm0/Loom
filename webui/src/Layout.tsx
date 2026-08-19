import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { useState } from "react";

const NAV = [
  { to: "/generate", label: "自动剪辑" },
  { to: "/tasks", label: "剪辑任务" },
  { to: "/materials", label: "素材库" },
  { to: "/tags", label: "标签管理" },
  { to: "/billing", label: "素材账单" },
  { to: "/settings", label: "基础配置" },
];

export function Layout() {
  const [collapsed, setCollapsed] = useState(false);
  const pathname = useRouterState({ select: (state) => state.location.pathname });

  return (
    <div className="app-shell">
      <aside className={`sidebar ${collapsed ? "collapsed" : ""}`}>
        <button className="sidebar-toggle" onClick={() => setCollapsed((value) => !value)}>
          {collapsed ? "展开" : "收起"}
        </button>
        <nav className="nav-list">
          {NAV.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              className={`nav-button ${pathname.startsWith(item.to) ? "active" : ""}`}
            >
              <span className="nav-label">{collapsed ? item.label.slice(0, 1) : item.label}</span>
            </Link>
          ))}
        </nav>
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
