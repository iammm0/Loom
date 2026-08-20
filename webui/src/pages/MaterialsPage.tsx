import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { statusLabel } from "../types";

type Material = {
  material_id: string;
  name?: string;
  filename?: string;
  status?: string;
  tags?: string[];
};

type MaterialList = {
  materials: Material[];
};

export function MaterialsPage() {
  const queryClient = useQueryClient();
  const [queryText, setQueryText] = useState("");
  const query = useQuery({
    queryKey: ["materials", queryText],
    queryFn: () =>
      api.get<MaterialList>(
        `/api/v1/materials?page=1&page_size=50&query=${encodeURIComponent(queryText)}`,
      ),
  });
  const upload = useMutation({
    mutationFn: (files: FileList) => {
      const body = new FormData();
      Array.from(files).forEach((file) => body.append("files", file));
      return api.upload("/api/v1/materials", body);
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["materials"] }),
  });
  const confirm = useMutation({
    mutationFn: (materialId: string) => api.post(`/api/v1/materials/${materialId}/confirm`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["materials"] }),
  });
  const analyze = useMutation({
    mutationFn: (materialId: string) => api.post(`/api/v1/materials/${materialId}/analyze`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["materials"] }),
  });
  const remove = useMutation({
    mutationFn: (materialId: string) => api.delete(`/api/v1/materials/${materialId}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["materials"] }),
  });

  return (
    <section className="page">
      <div className="toolbar">
        <input
          placeholder="搜索"
          value={queryText}
          onChange={(event) => setQueryText(event.target.value)}
        />
        <label className="ghost">
          上传
          <input
            type="file"
            multiple
            hidden
            onChange={(event) => event.target.files && upload.mutate(event.target.files)}
          />
        </label>
      </div>
      <div className="row-list">
        {(query.data?.materials || []).map((item) => (
          <div className="row-item" key={item.material_id}>
            <span className="row-title">
              {item.name || item.filename || item.material_id}
              {item.tags?.length ? <span className="muted"> · {item.tags.join("、")}</span> : null}
            </span>
            <span className="status">{statusLabel(item.status) || item.status}</span>
            <div className="row-actions">
              <button className="ghost" onClick={() => confirm.mutate(item.material_id)}>
                确认
              </button>
              <button className="ghost" onClick={() => analyze.mutate(item.material_id)}>
                打标
              </button>
              <button className="danger" onClick={() => remove.mutate(item.material_id)}>
                删除
              </button>
            </div>
          </div>
        ))}
        {!query.data?.materials?.length ? <div className="muted">暂无素材</div> : null}
      </div>
    </section>
  );
}
