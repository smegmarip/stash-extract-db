import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { api } from "@/lib/api";

// Resolves /records/r/:record_id → /records/<job_id>/<result_index>.
// Query results carry record_id (canonical, content-derived) but not the
// (job_id, result_index) pair the detail route is keyed on; this thin
// component closes that gap. Uses navigate(replace=true) so the canonical
// URL is what shows in the address bar, not /records/r/<uuid>.
export default function RecordByUuidRedirect() {
  const { record_id } = useParams();
  const navigate = useNavigate();

  const { data, isLoading, error } = useQuery({
    queryKey: ["record-by-uuid", record_id],
    queryFn: () => api.getRecordByUuid(record_id!),
    enabled: !!record_id,
  });

  useEffect(() => {
    if (data?.job_id && typeof data.result_index === "number") {
      navigate(`/records/${encodeURIComponent(data.job_id)}/${data.result_index}`, {
        replace: true,
      });
    }
  }, [data, navigate]);

  if (!record_id) return <Navigate to="/records" replace />;
  if (isLoading) {
    return <div className="text-center py-8 text-[var(--text-secondary)]">Resolving record…</div>;
  }
  if (error) {
    return (
      <div className="text-center py-8 text-rose-500">
        Record not found: <code className="font-mono">{record_id}</code>
      </div>
    );
  }
  return null;
}
