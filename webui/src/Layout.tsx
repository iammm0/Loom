import { Link, Outlet, useNavigate, useRouterState } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  const navigate = useNavigate();
  const queryClient = useQueryClient();
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
  const renameConversation = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      api.patch(`/api/v1/conversations/${id}`, { title }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["conversations"] }),
  });
  const retryConversation = useMutation({
    mutationFn: (id: string) => api.post(`/api/v1/conversations/${id}/retry`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
  const deleteConversation = useMutation({
    mutationFn: (id: string) => api.delete(`/api/v1/conversations/${id}`),
    onSuccess: (_data, id) => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      if (search.c === id) {
        void navigate({ to: "/generate", search: { new: true } });
      }
    },
  });

  return (
    <div className={`app-shell ${collapsed ? "collapsed" : ""}`}>
      <aside className="sidebar">
        <div className="sidebar-top">
          <Link to="/generate" className="brand" title="Loom">
            <img className="brand-mark" src="/logo.png" alt="" />
            {collapsed ? null : <span>Loom</span>}
          </Link>
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
              <div
                key={item.conversation_id}
                className={`chat-row ${search.c === item.conversation_id ? "active" : ""}`}
              >
                <Link
                  to="/generate"
                  search={{ c: item.conversation_id }}
                  className="chat-link"
                  title={item.title}
                >
                  {item.title || "未命名剪辑"}
                </Link>
                <div className="chat-actions">
                  <button
                    type="button"
                    className="chat-action"
                    title="重命名"
                    onClick={() => {
                      const title = window.prompt("对话名称", item.title || "");
                      if (title?.trim()) {
                        renameConversation.mutate({
                          id: item.conversation_id,
                          title: title.trim(),
                        });
                      }
                    }}
                  >
                    改
                  </button>
                  <button
                    type="button"
                    className="chat-action"
                    title="重试失败任务"
                    onClick={() => retryConversation.mutate(item.conversation_id)}
                  >
                    重试
                  </button>
                  <button
                    type="button"
                    className="chat-action danger"
                    title="删除对话"
                    onClick={() => {
                      if (window.confirm(`删除对话「${item.title || "未命名剪辑"}」？`)) {
                        deleteConversation.mutate(item.conversation_id);
                      }
                    }}
                  >
                    删
                  </button>
                </div>
              </div>
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
