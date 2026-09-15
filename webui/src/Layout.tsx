import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api";
import { Icons } from "./icons";
import type { ConversationSummary } from "./types";

const NAV = [
  { to: "/generate", label: "自动剪辑", icon: Icons.sparkle, event: "loom-new-session" },
  { to: "/tasks", label: "任务", icon: Icons.list },
  { to: "/publish", label: "自动发布", icon: Icons.publish },
  { to: "/materials", label: "素材", icon: Icons.folder },
  { to: "/tags", label: "标签", icon: Icons.tag },
  { to: "/billing", label: "账单", icon: Icons.receipt },
  { to: "/settings", label: "设置", icon: Icons.settings },
];

export function Layout() {
  const [collapsed, setCollapsed] = useState(false);
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const search = useRouterState({ select: (state) => state.location.search }) as {
    c?: string;
  };
  const flush = pathname.startsWith("/generate");
  const conversations = useQuery({
    queryKey: ["conversations"],
    queryFn: () => api.get<{ conversations: ConversationSummary[] }>("/api/v1/conversations"),
    refetchInterval: 4000,
  });

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
                onClick={(event) => {
                  if (item.event && pathname.startsWith(item.to)) {
                    event.preventDefault();
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
        {collapsed ? null : (
          <div className="sidebar-chats">
            <div className="sidebar-chats-label">对话</div>
            {(conversations.data?.conversations || []).map((item) => (
              <Link
                key={item.conversation_id}
                to="/generate"
                search={{ c: item.conversation_id }}
                className={`chat-link ${search.c === item.conversation_id ? "active" : ""}`}
                title={item.title}
              >
                {item.title || "未命名剪辑"}
              </Link>
            ))}
          </div>
        )}
      </aside>
      <main className={`content ${flush ? "flush" : ""}`}>
        <Outlet />
      </main>
    </div>
  );
}
