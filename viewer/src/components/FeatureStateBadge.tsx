import { Link } from "react-router-dom";
import { Badge } from "./Badge";

interface FeatureStateBadgeProps {
  state: string | null;
  progress: number | null;
  jobId: string;
}

/**
 * Passive badge — never blocks browse. Per CLAUDE.md §14, the 503 contract
 * applies to /match/* only; record display is independent of featurization.
 */
export function FeatureStateBadge({ state, progress, jobId }: FeatureStateBadgeProps) {
  const statusHref = `/status?job_id=${encodeURIComponent(jobId)}`;

  if (state === "ready") {
    return (
      <Link to={statusHref}>
        <Badge variant="success" title="Match index ready">Match index: ready</Badge>
      </Link>
    );
  }
  if (state === "failed") {
    return (
      <Link to={statusHref}>
        <Badge variant="danger" title="Featurization failed">Match index: failed</Badge>
      </Link>
    );
  }
  if (state === "featurizing") {
    const pct = progress != null ? Math.round(progress * 100) : 0;
    return (
      <Link to={statusHref}>
        <Badge variant="warning" title="Featurization in progress">
          Match index: featurizing {pct}%
        </Badge>
      </Link>
    );
  }
  return (
    <Link to={statusHref}>
      <Badge variant="outline" title="Not yet featurized">Match index: pending</Badge>
    </Link>
  );
}
