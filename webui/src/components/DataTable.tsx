import {
  flexRender,
  type Table as ReactTable,
} from "@tanstack/react-table";

export function DataTable<T>({ table, empty }: { table: ReactTable<T>; empty?: string }) {
  const rows = table.getRowModel().rows;
  return (
    <table>
      <thead>
        {table.getHeaderGroups().map((group) => (
          <tr key={group.id}>
            {group.headers.map((header) => (
              <th key={header.id}>
                {flexRender(header.column.columnDef.header, header.getContext())}
              </th>
            ))}
          </tr>
        ))}
      </thead>
      <tbody>
        {rows.length ? (
          rows.map((row) => (
            <tr key={row.id}>
              {row.getVisibleCells().map((cell) => (
                <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
              ))}
            </tr>
          ))
        ) : (
          <tr>
            <td colSpan={table.getAllColumns().length} className="muted">
              {empty || "暂无数据"}
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}
