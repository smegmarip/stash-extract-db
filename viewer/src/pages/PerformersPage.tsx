import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, PerformerSummary } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/Card";
import { Badge } from "@/components/Badge";
import { SearchInput } from "@/components/SearchInput";
import { Pagination } from "@/components/Pagination";
import { Select } from "@/components/Select";
import { useDebounce } from "@/hooks/useDebounce";

const ALL_JOBS = "__all__";

export default function PerformersPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const page = parseInt(searchParams.get("page") || "1", 10);
  const jobId = searchParams.get("job_id") || ALL_JOBS;

  const urlQ = searchParams.get("q") || "";
  const [searchInput, setSearchInput] = useState(urlQ);
  const debounced = useDebounce(searchInput, 300);

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
    queryKey: ["performers", jobId, urlQ, page],
    queryFn: () =>
      api.listPerformers({
        job_id: jobId === ALL_JOBS ? undefined : jobId,
        q: urlQ || undefined,
        page, limit: 24,
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
        <h1 className="text-2xl font-bold">Performers</h1>
        <p className="text-[var(--text-secondary)] mt-1">
          Performers across cached extractor records.
        </p>
      </div>

      <div className="mb-4 flex flex-wrap gap-3 items-center">
        <SearchInput
          value={searchInput}
          onChange={setSearchInput}
          placeholder="Search performers by name..."
          className="flex-1 min-w-[16rem] max-w-xl"
        />
        <Select
          ariaLabel="Filter by job"
          value={jobId}
          onValueChange={(v) => updateParam("job_id", v === ALL_JOBS ? null : v, true)}
          options={jobOptions}
        />
      </div>

      {isLoading ? (
        <div className="text-center py-8 text-[var(--text-secondary)]">Loading performers...</div>
      ) : error ? (
        <div className="text-center py-8 text-rose-500">
          Error loading performers: {(error as Error).message}
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
            {(data?.data || []).map((p) => (
              <PerformerCard
                key={p.name_lower}
                performer={p}
                onClick={() => navigate(`/performers/${encodeURIComponent(p.name_lower)}`)}
              />
            ))}
          </div>

          {(data?.data?.length || 0) === 0 ? (
            <div className="text-center py-12 text-[var(--text-secondary)]">
              {urlQ ? "No performers match your search." : "No performers indexed yet."}
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

function PerformerCard({
  performer, onClick,
}: { performer: PerformerSummary; onClick: () => void }) {
  return (
    <Card onClick={onClick}>
      <CardHeader>
        <CardTitle className="line-clamp-1">{performer.name_display}</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="flex items-center gap-2 flex-wrap text-sm">
          <Badge variant="default">
            {performer.record_count} record{performer.record_count !== 1 ? "s" : ""}
          </Badge>
          <Badge variant="secondary">
            {performer.job_count} job{performer.job_count !== 1 ? "s" : ""}
          </Badge>
        </div>
      </CardContent>
    </Card>
  );
}
