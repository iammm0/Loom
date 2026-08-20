import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { useState } from "react";
import { Icons } from "./icons";

const NAV = [
  { to: "/generate", label: "自动剪辑", icon: Icons.sparkle, event: "loom-new-session" },
  { to: "/tasks", label: "任务", icon: Icons.list },
  { to: "/materials", label: "素材", icon: Icons.folder },
  { to: "/tags", label: "标签", icon: Icons.tag },
  { to: "/billing", label: "账单", icon: Icons.receipt },
  { to: "/settings", label: "设置", icon: Icons.settings },
];

export function Layout() {
  const [collapsed, setCollapsed] = useState(false);
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const flush = pathname.startsWith("/generate");

  return (
    <div className={`app-shell ${collapsed ? "collapsed" : ""}`}>
      <aside className="sidebar">
        <div className="sidebar-top">
          {collapsed ? null : <span className="brand">Loom</span>}
          <button
            className="icon-btn"
            type="button"
            onClick={() => setCollapsed((value) => !value)}
            aria-label={collapsed ? "展开" : "收起"}
          >
            <Icons.panel />
          </button>
        </div>
        <nav className="nav-list">
          {NAV.map((item) => {
            const Icon = item.icon;
            return (
              <Link
                key={item.to}
                to={item.to}
                className={`nav-button ${pathname.startsWith(item.to) ? "active" : ""}`}
                title={item.label}
                onClick={() => {
                  if (item.event && pathname.startsWith(item.to)) {
                    window.dispatchEvent(new Event(item.event));
                  }
                }}
              >
                <Icon />
                {collapsed ? null : <span>{item.label}</span>}
              </Link>
            );
          })}
        </nav>
      </aside>
      <main className={`content ${flush ? "flush" : ""}`}>
        <Outlet />
      </main>
    </div>
  );
}
