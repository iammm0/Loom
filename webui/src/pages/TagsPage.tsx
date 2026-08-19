import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createColumnHelper,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { api } from "../api";
import { DataTable } from "../components/DataTable";

type Tag = {
  name: string;
  material_count?: number;
};

const columnHelper = createColumnHelper<Tag>();

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
  const columns = useMemo(
    () => [
      columnHelper.accessor("name", { header: "标签" }),
      columnHelper.accessor("material_count", { header: "数量" }),
      columnHelper.display({
        id: "actions",
        header: "",
        cell: ({ row }) => (
          <div className="toolbar">
            <button
              className="ghost"
              onClick={() => {
                const name = window.prompt("新名称", row.original.name);
                if (name) rename.mutate({ oldName: row.original.name, name });
              }}
            >
              重命名
            </button>
            <button className="danger" onClick={() => remove.mutate(row.original.name)}>
              删除
            </button>
          </div>
        ),
      }),
    ],
    [remove, rename],
  );
  const table = useReactTable({
    data: query.data?.tags || [],
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <section className="page">
      <div className="card grid">
        <input
          placeholder="搜索标签"
          value={queryText}
          onChange={(event) => setQueryText(event.target.value)}
        />
        <DataTable table={table} empty="暂无标签" />
      </div>
    </section>
  );
}
