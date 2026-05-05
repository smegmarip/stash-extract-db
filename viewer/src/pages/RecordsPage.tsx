import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api, RecordsListParams } from "@/lib/api";
import { RecordCard } from "@/components/RecordCard";
import { SearchInput } from "@/components/SearchInput";
import { Pagination } from "@/components/Pagination";
import { Select } from "@/components/Select";
import { useDebounce } from "@/hooks/useDebounce";

const SORT_OPTIONS = [
  { value: "id", label: "Sort: ID" },
  { value: "title", label: "Sort: Title" },
  { value: "date", label: "Sort: Date" },
  { value: "performer", label: "Sort: Performer" },
];

const DIR_OPTIONS = [
  { value: "asc", label: "Ascending" },
  { value: "desc", label: "Descending" },
];

const ALL_JOBS = "__all__";

export default function RecordsPage() {
  const [searchParams, setSearchParams] = useSearchParams();

  const page = parseInt(searchParams.get("page") || "1", 10);
  const jobId = searchParams.get("job_id") || ALL_JOBS;
  const sort = (searchParams.get("sort") || "id") as RecordsListParams["sort"];
  const dir = (searchParams.get("dir") || "asc") as RecordsListParams["dir"];

  // Local search input (debounced before propagating to URL).
  const urlQ = searchParams.get("q") || "";
  const [searchInput, setSearchInput] = useState(urlQ);
  const debounced = useDebounce(searchInput, 300);

  // When debounced value changes, push it into the URL (resets page to 1).
  useEffect(() => {
    if (debounced === urlQ) return;
    const next = new URLSearchParams(searchParams);
    if (debounced) next.set("q", debounced);
    else next.delete("q");
    next.set("page", "1");
    setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced]);

  const { data: jobs } = useQuery({
    queryKey: ["jobs"],
    queryFn: api.listJobs,
  });

  const { data, isLoading, error } = useQuery({
    queryKey: ["records", jobId, urlQ, sort, dir, page],
    queryFn: () =>
      api.listRecords({
        job_id: jobId === ALL_JOBS ? undefined : jobId,
        q: urlQ || undefined,
        sort, dir, page, limit: 24,
      }),
  });

  const updateParam = (key: string, value: string | null, resetPage = false) => {
    const next = new URLSearchParams(searchParams);
    if (value === null || value === "") next.delete(key);
    else next.set(key, value);
    if (resetPage) next.set("page", "1");
    setSearchParams(next);
  };

  const jobOptions = [
    { value: ALL_JOBS, label: "All jobs" },
    ...((jobs || []).map((j) => ({
      value: j.job_id,
      label: `${j.job_name} (${j.record_count})`,
    }))),
  ];

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-bold">Records</h1>
        <p className="text-[var(--text-secondary)] mt-1">
          Extractor records indexed by stash-extract-db.
        </p>
      </div>

      <div className="mb-4 flex flex-wrap gap-3 items-center">
        <SearchInput
          value={searchInput}
          onChange={setSearchInput}
          placeholder="Search id / title / url / performer..."
          className="flex-1 min-w-[16rem] max-w-xl"
        />
        <Select
          ariaLabel="Filter by job"
          value={jobId}
          onValueChange={(v) => updateParam("job_id", v === ALL_JOBS ? null : v, true)}
          options={jobOptions}
        />
        <Select
          ariaLabel="Sort by"
          value={sort || "id"}
          onValueChange={(v) => updateParam("sort", v, true)}
          options={SORT_OPTIONS}
        />
        <Select
          ariaLabel="Sort direction"
          value={dir || "asc"}
          onValueChange={(v) => updateParam("dir", v, true)}
          options={DIR_OPTIONS}
        />
      </div>

      {isLoading ? (
        <div className="text-center py-8 text-[var(--text-secondary)]">Loading records...</div>
      ) : error ? (
        <div className="text-center py-8 text-rose-500">
          Error loading records: {(error as Error).message}
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {(data?.data || []).map((rec) => (
              <RecordCard key={`${rec.job_id}:${rec.result_index}`} record={rec} />
            ))}
          </div>

          {(data?.data?.length || 0) === 0 ? (
            <div className="text-center py-12 text-[var(--text-secondary)]">
              {urlQ ? "No records match your search." : "No records to show."}
            </div>
          ) : null}

          <div className="mt-6">
            <Pagination
              page={data?.pagination.page || 1}
              totalPages={data?.pagination.totalPages || 0}
              total={data?.pagination.total || 0}
              limit={data?.pagination.limit || 24}
              onPageChange={(p) => updateParam("page", String(p))}
            />
          </div>
        </>
      )}
    </div>
  );
}

