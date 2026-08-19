import { useQuery } from "@tanstack/react-query";
import {
  createColumnHelper,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { useMemo } from "react";
import { api } from "../api";
import { DataTable } from "../components/DataTable";

type BillingRow = Record<string, string | number | undefined>;

type Billing = {
  display_rows: BillingRow[];
  total_cost_cny?: number;
};

const columnHelper = createColumnHelper<BillingRow>();
const COLUMNS = [
  "账单记录时间",
  "作品主题",
  "分镜",
  "模型",
  "规格",
  "请求时长（秒）",
  "状态",
  "金额（元）",
  "计算依据",
] as const;

export function BillingPage() {
  const query = useQuery({
    queryKey: ["billing"],
    queryFn: () => api.get<Billing>("/api/v1/billing/seedance"),
  });
  const columns = useMemo(
    () =>
      COLUMNS.map((key) =>
        columnHelper.accessor((row) => row[key], {
          id: key,
          header: key.replace("（元）", "").replace("（秒）", ""),
        }),
      ),
    [],
  );
  const table = useReactTable({
    data: query.data?.display_rows || [],
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <section className="page">
      <div className="card">
        <p>合计 {query.data?.total_cost_cny ?? 0} 元</p>
        <DataTable table={table} empty="暂无账单" />
      </div>
    </section>
  );
}
