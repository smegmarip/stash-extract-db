import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "@/lib/api";
import { Badge } from "@/components/Badge";
import { RecordCard } from "@/components/RecordCard";

export default function PerformerDetailPage() {
  const { name } = useParams();
  const decoded = name ? decodeURIComponent(name) : "";

  const { data: performer, isLoading, error } = useQuery({
    queryKey: ["performer", decoded],
    queryFn: () => api.getPerformer(decoded),
    enabled: !!decoded,
  });

  if (isLoading) {
    return <div className="text-center py-8 text-[var(--text-secondary)]">Loading performer...</div>;
  }
  if (error || !performer) {
    return (
      <div className="text-center py-8 text-rose-500">
        Error loading performer: {(error as Error)?.message || "not found"}
      </div>
    );
  }

  return (
    <div>
      <div className="mb-6">
        <Link to="/performers" className="text-[var(--accent)] hover:underline text-sm">
          &larr; Back to Performers
        </Link>
        <h1 className="text-2xl font-bold mt-2">{performer.name_display}</h1>
        <div className="mt-2 flex items-center gap-2 flex-wrap">
          <Badge variant="default">
            {performer.record_count} record{performer.record_count !== 1 ? "s" : ""}
          </Badge>
          <Badge variant="secondary">
            {performer.job_count} job{performer.job_count !== 1 ? "s" : ""}
          </Badge>
        </div>
      </div>

      <div className="space-y-8">
        {performer.jobs.map((job) => (
          <section key={job.job_id}>
            <div className="mb-3 flex items-baseline gap-3 flex-wrap">
              <h2 className="text-lg font-semibold">{job.job_name}</h2>
              <span className="text-xs text-[var(--text-secondary)] font-mono">{job.job_id}</span>
              <Badge variant="outline">
                {job.records.length} record{job.records.length !== 1 ? "s" : ""}
              </Badge>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {job.records.map((rec) => (
                <RecordCard
                  key={`${rec.job_id}:${rec.result_index}`}
                  record={rec}
                  showJobBadge={false}
                />
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
