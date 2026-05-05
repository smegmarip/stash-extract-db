import { cn } from "@/lib/cn";

interface PaginationProps {
  page: number;
  totalPages: number;
  total: number;
  limit: number;
  onPageChange: (page: number) => void;
}

export function Pagination({ page, totalPages, total, limit, onPageChange }: PaginationProps) {
  const start = total === 0 ? 0 : (page - 1) * limit + 1;
  const end = Math.min(page * limit, total);

  const getPageNumbers = (): (number | "ellipsis")[] => {
    const pages: (number | "ellipsis")[] = [];
    const showPages = 5;
    if (totalPages <= showPages + 2) {
      for (let i = 1; i <= totalPages; i++) pages.push(i);
    } else {
      pages.push(1);
      if (page > 3) pages.push("ellipsis");
      const startPage = Math.max(2, page - 1);
      const endPage = Math.min(totalPages - 1, page + 1);
      for (let i = startPage; i <= endPage; i++) pages.push(i);
      if (page < totalPages - 2) pages.push("ellipsis");
      if (totalPages > 1) pages.push(totalPages);
    }
    return pages;
  };

  if (totalPages <= 1) {
    return (
      <div className="text-sm text-[var(--text-secondary)]">
        Showing {total} {total === 1 ? "item" : "items"}
      </div>
    );
  }

  return (
    <div className="flex items-center justify-between gap-4 flex-wrap">
      <div className="text-sm text-[var(--text-secondary)]">
        Showing {start}-{end} of {total}
      </div>
      <div className="flex items-center gap-1">
        <button
          onClick={() => onPageChange(page - 1)}
          disabled={page === 1}
          className={cn(
            "px-3 py-1.5 text-sm rounded-md transition-colors",
            page === 1
              ? "text-[var(--text-secondary)] cursor-not-allowed"
              : "hover:bg-[var(--bg-tertiary)] text-[var(--text-primary)]",
          )}
        >
          Prev
        </button>
        {getPageNumbers().map((p, idx) =>
          p === "ellipsis" ? (
            <span key={`ellipsis-${idx}`} className="px-2 text-[var(--text-secondary)]">
              ...
            </span>
          ) : (
            <button
              key={p}
              onClick={() => onPageChange(p)}
              className={cn(
                "px-3 py-1.5 text-sm rounded-md transition-colors min-w-[36px]",
                p === page
                  ? "bg-[var(--accent)] text-white"
                  : "hover:bg-[var(--bg-tertiary)] text-[var(--text-primary)]",
              )}
            >
              {p}
            </button>
          ),
        )}
        <button
          onClick={() => onPageChange(page + 1)}
          disabled={page === totalPages}
          className={cn(
            "px-3 py-1.5 text-sm rounded-md transition-colors",
            page === totalPages
              ? "text-[var(--text-secondary)] cursor-not-allowed"
              : "hover:bg-[var(--bg-tertiary)] text-[var(--text-primary)]",
          )}
        >
          Next
        </button>
      </div>
    </div>
  );
}
