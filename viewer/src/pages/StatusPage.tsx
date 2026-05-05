import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api, JobSummary, FleetStatus } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/Card";
import { Badge } from "@/components/Badge";
import { Select } from "@/components/Select";

const STATE_FILTERS = [
  { value: "all", label: "All states" },
  { value: "ready", label: "Ready" },
  { value: "featurizing", label: "Featurizing" },
  { value: "queued", label: "Queued" },
  { value: "failed", label: "Failed" },
  { value: "none", label: "Pending (no row)" },
];

const POLL_INTERVAL_MS = 5_000;

export default function StatusPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const initialJobId = searchParams.get("job_id") || "";
  const [stateFilter, setStateFilter] = useState<string>("all");
  const [jobIdFilter, setJobIdFilter] = useState<string>(initialJobId);

  // Poll while fleet has anything queued or in flight; pause otherwise so we
  // don't hammer the bridge once the system is at rest.
  const fleet = useQuery<FleetStatus>({
    queryKey: ["fleet"],
    queryFn: api.fleetStatus,
    refetchInterval: (q) => {
      const data = q.state.data as FleetStatus | undefined;
      if (!data) return POLL_INTERVAL_MS;
      const active = (data.queued || 0) + (data.in_progress || 0);
      return active > 0 ? POLL_INTERVAL_MS : false;
    },
  });

  const jobs = useQuery<JobSummary[]>({
    queryKey: ["jobs"],
    queryFn: api.listJobs,
    refetchInterval: (q) => {
      const data = fleet.data;
      if (!data) return POLL_INTERVAL_MS;
      const active = (data.queued || 0) + (data.in_progress || 0);
      return active > 0 ? POLL_INTERVAL_MS : false;
      void q;
    },
  });

  const filtered = useMemo(() => {
    const all = jobs.data || [];
    return all.filter((j) => {
      if (jobIdFilter && !j.job_id.includes(jobIdFilter) && !j.job_name.toLowerCase().includes(jobIdFilter.toLowerCase())) {
        return false;
      }
      if (stateFilter === "all") return true;
      if (stateFilter === "queued") {
        return j.feature_state === "featurizing" && (j.feature_progress ?? 0) === 0;
      }
      if (stateFilter === "featurizing") {
        return j.feature_state === "featurizing" && (j.feature_progress ?? 0) > 0;
      }
      if (stateFilter === "none") return j.feature_state === null;
      return j.feature_state === stateFilter;
    });
  }, [jobs.data, stateFilter, jobIdFilter]);

  return (
    <div>
      <div className="mb-6 flex items-baseline gap-3 flex-wrap">
        <h1 className="text-2xl font-bold">Status</h1>
        <p className="text-[var(--text-secondary)]">
          Featurization lifecycle (CLAUDE.md §14). {fleetIsActive(fleet.data) ? "Polling…" : "Idle."}
        </p>
      </div>

      <FleetPanel fleet={fleet.data} loading={fleet.isLoading} error={fleet.error as Error | null} />

      <div className="mt-8 mb-3 flex items-baseline justify-between gap-3 flex-wrap">
        <h2 className="text-lg font-semibold">Per-job</h2>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={jobIdFilter}
            onChange={(e) => {
              setJobIdFilter(e.target.value);
              const next = new URLSearchParams(searchParams);
              if (e.target.value) next.set("job_id", e.target.value);
              else next.delete("job_id");
              setSearchParams(next, { replace: true });
            }}
            placeholder="Filter by job id / name..."
            className="px-3 py-2 rounded-md text-sm bg-[var(--bg-tertiary)] border border-[var(--border)] focus:outline-none focus:ring-2 focus:ring-[var(--accent)] min-w-[14rem]"
          />
          <Select
            ariaLabel="Filter by state"
            value={stateFilter}
            onValueChange={setStateFilter}
            options={STATE_FILTERS}
          />
        </div>
      </div>

      {jobs.isLoading ? (
        <div className="text-center py-8 text-[var(--text-secondary)]">Loading jobs...</div>
      ) : jobs.error ? (
        <div className="text-center py-8 text-rose-500">
          Error loading jobs: {(jobs.error as Error).message}
        </div>
      ) : (
        <PerJobTable jobs={filtered} />
      )}
    </div>
  );
}

function fleetIsActive(f: FleetStatus | undefined): boolean {
  if (!f) return false;
  return (f.queued || 0) + (f.in_progress || 0) > 0;
}

function FleetPanel({
  fleet, loading, error,
}: { fleet: FleetStatus | undefined; loading: boolean; error: Error | null }) {
  if (loading && !fleet) {
    return <div className="text-[var(--text-secondary)]">Loading fleet status...</div>;
  }
  if (error) {
    return <div className="text-rose-500">Error loading fleet status: {error.message}</div>;
  }
  if (!fleet) return null;

  const cells = [
    { label: "Ready", value: fleet.ready, variant: "success" as const },
    { label: "In progress", value: fleet.in_progress, variant: "warning" as const },
    { label: "Queued", value: fleet.queued, variant: "secondary" as const },
    { label: "Failed", value: fleet.failed, variant: "danger" as const },
  ];

  return (
    <Card>
      <CardHeader>
        <CardTitle>Fleet</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          {cells.map((c) => (
            <div
              key={c.label}
              className="bg-[var(--bg-tertiary)] rounded-md px-3 py-3 flex items-center justify-between"
            >
              <span className="text-sm text-[var(--text-secondary)]">{c.label}</span>
              <Badge variant={c.variant}>{c.value}</Badge>
            </div>
          ))}
        </div>
        <div className="mt-3 text-xs text-[var(--text-secondary)] flex gap-4 flex-wrap">
          <span>Concurrency limit: {fleet.concurrency_limit}</span>
          <span>Lifecycle: {fleet.lifecycle_enabled ? "enabled" : "disabled"}</span>
        </div>
      </CardContent>
    </Card>
  );
}

function PerJobTable({ jobs }: { jobs: JobSummary[] }) {
  if (jobs.length === 0) {
    return (
      <div className="text-center py-12 text-[var(--text-secondary)]">No jobs match.</div>
    );
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
      <table className="w-full text-sm">
        <thead className="bg-[var(--bg-secondary)] text-[var(--text-secondary)]">
          <tr className="text-left">
            <th className="px-3 py-2">Job</th>
            <th className="px-3 py-2">Records</th>
            <th className="px-3 py-2">State</th>
            <th className="px-3 py-2 w-48">Progress</th>
            <th className="px-3 py-2">Started</th>
            <th className="px-3 py-2">Finished</th>
            <th className="px-3 py-2">Error</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.job_id} className="border-t border-[var(--border)]">
              <td className="px-3 py-2">
                <div className="font-medium">{j.job_name}</div>
                <div className="text-xs font-mono text-[var(--text-secondary)]">{j.job_id}</div>
              </td>
              <td className="px-3 py-2">{j.record_count}</td>
              <td className="px-3 py-2">
                <StateBadge job={j} />
              </td>
              <td className="px-3 py-2">
                <ProgressBar progress={j.feature_progress} state={j.feature_state} />
              </td>
              <td className="px-3 py-2 font-mono text-xs text-[var(--text-secondary)]">
                {j.feature_started_at || "-"}
              </td>
              <td className="px-3 py-2 font-mono text-xs text-[var(--text-secondary)]">
                {j.feature_finished_at || "-"}
              </td>
              <td className="px-3 py-2 text-xs text-rose-500 max-w-xs truncate" title={j.feature_error || ""}>
                {j.feature_error || ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StateBadge({ job }: { job: JobSummary }) {
  if (job.feature_state === "ready") return <Badge variant="success">ready</Badge>;
  if (job.feature_state === "failed") return <Badge variant="danger">failed</Badge>;
  if (job.feature_state === "featurizing") {
    const inFlight = (job.feature_progress ?? 0) > 0;
    return (
      <Badge variant="warning">{inFlight ? "featurizing" : "queued"}</Badge>
    );
  }
  return <Badge variant="outline">pending</Badge>;
}

function ProgressBar({
  progress, state,
}: { progress: number | null; state: string | null }) {
  if (state === null) return <span className="text-xs text-[var(--text-secondary)]">-</span>;
  if (state === "ready") return <span className="text-xs">100%</span>;
  if (state === "failed") return <span className="text-xs text-rose-500">-</span>;
  const pct = Math.round((progress || 0) * 100);
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-2 bg-[var(--bg-tertiary)] rounded-full overflow-hidden">
        <div
          className="h-full bg-[var(--accent)] transition-all"
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-xs w-10 text-right">{pct}%</span>
    </div>
  );
}
