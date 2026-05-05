import { useNavigate } from "react-router-dom";
import { RecordSummary } from "@/lib/api";
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "./Card";
import { Badge } from "./Badge";

interface RecordCardProps {
  record: RecordSummary;
  showJobBadge?: boolean;
}

export function RecordCard({ record, showJobBadge = true }: RecordCardProps) {
  const navigate = useNavigate();
  const previewUrl = record.cover_image || record.images[0] || null;

  const onClick = () =>
    navigate(`/records/${encodeURIComponent(record.job_id)}/${record.result_index}`);

  return (
    <Card onClick={onClick} className="flex flex-col">
      <div className="aspect-video bg-[var(--bg-tertiary)] flex items-center justify-center">
        {previewUrl ? (
          <img
            src={previewUrl}
            alt={record.title || "preview"}
            loading="lazy"
            className="w-full h-full object-cover"
            onError={(e) => {
              (e.currentTarget as HTMLImageElement).style.display = "none";
            }}
          />
        ) : (
          <span className="text-xs text-[var(--text-secondary)]">no image</span>
        )}
      </div>
      <CardHeader>
        <CardTitle className="line-clamp-1">{record.title || "Untitled"}</CardTitle>
        <CardDescription className="line-clamp-2">
          {record.details || "No details"}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="flex items-center flex-wrap gap-x-2 gap-y-1 text-xs text-[var(--text-secondary)]">
          {record.date ? <span>{record.date}</span> : null}
          {record.date && record.performer_count > 0 ? <span>·</span> : null}
          {record.performer_count > 0 ? (
            <span>
              {record.performer_count} performer{record.performer_count !== 1 ? "s" : ""}
            </span>
          ) : null}
          {(record.date || record.performer_count > 0) && record.image_count > 0 ? <span>·</span> : null}
          {record.image_count > 0 ? (
            <span>
              {record.image_count} image{record.image_count !== 1 ? "s" : ""}
            </span>
          ) : null}
        </div>
        <div className="mt-2 flex items-center gap-2 flex-wrap">
          {showJobBadge ? (
            <Badge variant="outline" title={record.job_id} className="line-clamp-1 max-w-full">
              {record.job_name}
            </Badge>
          ) : null}
          {record.id ? <Badge variant="secondary">#{record.id}</Badge> : null}
        </div>
      </CardContent>
    </Card>
  );
}
