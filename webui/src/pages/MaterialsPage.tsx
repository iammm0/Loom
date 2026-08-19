import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createColumnHelper,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { api } from "../api";
import { DataTable } from "../components/DataTable";

type Material = {
  material_id: string;
  name?: string;
  filename?: string;
  status?: string;
  tags?: string[];
};

type MaterialList = {
  materials: Material[];
  total: number;
};

const columnHelper = createColumnHelper<Material>();

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

  const columns = useMemo(
    () => [
      columnHelper.accessor((row) => row.name || row.filename || row.material_id, {
        id: "name",
        header: "文件",
      }),
      columnHelper.accessor("status", { header: "状态" }),
      columnHelper.accessor("tags", {
        header: "标签",
        cell: (info) => (info.getValue() || []).join("、"),
      }),
      columnHelper.display({
        id: "actions",
        header: "",
        cell: ({ row }) => (
          <div className="toolbar">
            <button className="ghost" onClick={() => confirm.mutate(row.original.material_id)}>
              确认
            </button>
            <button className="ghost" onClick={() => analyze.mutate(row.original.material_id)}>
              重新打标
            </button>
            <button className="danger" onClick={() => remove.mutate(row.original.material_id)}>
              删除
            </button>
          </div>
        ),
      }),
    ],
    [analyze, confirm, remove],
  );
  const table = useReactTable({
    data: query.data?.materials || [],
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <section className="page">
      <div className="card toolbar">
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
      <div className="card">
        <DataTable table={table} empty="暂无素材" />
      </div>
    </section>
  );
}
