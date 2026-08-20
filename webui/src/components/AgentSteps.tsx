import { Icons } from "../icons";
import type { StreamStep } from "../types";

function Glyph({ state }: { state: string }) {
  if (state === "running" || state === "waiting") {
    return <span className="tool-spinner" />;
  }
  if (state === "failed") {
    return <Icons.x className="tool-glyph failed" />;
  }
  if (state === "cancelled") {
    return <Icons.x className="tool-glyph muted" />;
  }
  if (state === "completed") {
    return <Icons.check className="tool-glyph ok" />;
  }
  return <span className="tool-dot" />;
}

export function AgentSteps({ steps }: { steps?: StreamStep[] }) {
  const visible = (steps || []).filter((step) => step.state !== "pending");
  if (!visible.length) {
    return (
      <div className="tool-row running">
        <span className="tool-spinner" />
        <span>处理中</span>
      </div>
    );
  }
  return (
    <div className="tool-list">
      {visible.map((step) => (
        <div className={`tool-row ${step.state}`} key={step.key}>
          <Glyph state={step.state} />
          <span className="tool-label">{step.label}</span>
          {step.meta ? <span className="tool-meta">{step.meta}</span> : null}
          <span className="tool-time">{step.elapsed_label || ""}</span>
        </div>
      ))}
    </div>
  );
}
