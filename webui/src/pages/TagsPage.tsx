import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";

type Tag = {
  name: string;
  material_count?: number;
};

export function TagsPage() {
  const queryClient = useQueryClient();
  const [queryText, setQueryText] = useState("");
  const query = useQuery({
    queryKey: ["tags", queryText],
    queryFn: () =>
      api.get<{ tags: Tag[] }>(`/api/v1/materials/tags?query=${encodeURIComponent(queryText)}`),
  });
  const rename = useMutation({
    mutationFn: ({ oldName, name }: { oldName: string; name: string }) =>
      api.patch(`/api/v1/materials/tags/${encodeURIComponent(oldName)}`, { name }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["tags"] }),
  });
  const remove = useMutation({
    mutationFn: (name: string) => api.delete(`/api/v1/materials/tags/${encodeURIComponent(name)}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["tags"] }),
  });

  return (
    <section className="page">
      <input
        placeholder="搜索"
        value={queryText}
        onChange={(event) => setQueryText(event.target.value)}
      />
      <div className="row-list">
        {(query.data?.tags || []).map((tag) => (
          <div className="row-item" key={tag.name}>
            <span className="row-title">{tag.name}</span>
            <span className="muted">{tag.material_count ?? 0}</span>
            <div className="row-actions">
              <button
                className="ghost"
                onClick={() => {
                  const name = window.prompt("名称", tag.name);
                  if (name) rename.mutate({ oldName: tag.name, name });
                }}
              >
                重命名
              </button>
              <button className="danger" onClick={() => remove.mutate(tag.name)}>
                删除
              </button>
            </div>
          </div>
        ))}
        {!query.data?.tags?.length ? <div className="muted">暂无标签</div> : null}
      </div>
    </section>
  );
}
