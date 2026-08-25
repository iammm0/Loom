import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createRootRoute,
  createRoute,
  createRouter,
  redirect,
} from "@tanstack/react-router";
import React from "react";
import ReactDOM from "react-dom/client";
import { Layout } from "./Layout";
import { BillingPage } from "./pages/BillingPage";
import { GeneratePage } from "./pages/GeneratePage";
import { MaterialsPage } from "./pages/MaterialsPage";
import { PublishPage } from "./pages/PublishPage";
import { SettingsPage } from "./pages/SettingsPage";
import { TaskDetailPage } from "./pages/TaskDetailPage";
import { TasksPage } from "./pages/TasksPage";
import { TagsPage } from "./pages/TagsPage";
import "./styles.css";

const rootRoute = createRootRoute({
  component: Layout,
});

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: () => {
    throw redirect({ to: "/generate" });
  },
});

const generateRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/generate",
  component: GeneratePage,
});

const tasksRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tasks",
  component: TasksPage,
});

const taskDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tasks/$taskId",
  component: TaskDetailPage,
});

const materialsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/materials",
  component: MaterialsPage,
});

const tagsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tags",
  component: TagsPage,
});

const publishRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/publish",
  component: PublishPage,
});

const billingRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/billing",
  component: BillingPage,
});

const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: SettingsPage,
});

const routeTree = rootRoute.addChildren([
  indexRoute,
  generateRoute,
  tasksRoute,
  taskDetailRoute,
  materialsRoute,
  tagsRoute,
  publishRoute,
  billingRoute,
  settingsRoute,
]);

const router = createRouter({ routeTree });
const queryClient = new QueryClient();

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </React.StrictMode>,
);
