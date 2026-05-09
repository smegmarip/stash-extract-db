import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/Card";
import { Badge } from "@/components/Badge";
import { Lightbox } from "@/components/Lightbox";
import { FeatureStateBadge } from "@/components/FeatureStateBadge";

export default function RecordDetailPage() {
  const { job_id, result_index } = useParams();
  const idx = result_index ? parseInt(result_index, 10) : NaN;

  const { data: record, isLoading, error } = useQuery({
    queryKey: ["record", job_id, idx],
    queryFn: () => api.getRecord(job_id!, idx),
    enabled: !!job_id && !Number.isNaN(idx),
  });

  const [lightboxSrc, setLightboxSrc] = useState<string | null>(null);

  if (isLoading) {
    return <div className="text-center py-8 text-[var(--text-secondary)]">Loading record...</div>;
  }

  if (error || !record) {
    return (
      <div className="text-center py-8 text-rose-500">
        Error loading record: {(error as Error)?.message || "not found"}
      </div>
    );
  }

  return (
    <div>
      <div className="mb-6">
        <Link to="/records" className="text-[var(--accent)] hover:underline text-sm">
          &larr; Back to Records
        </Link>
        <div className="mt-2 flex items-center gap-3 flex-wrap">
          <h1 className="text-2xl font-bold">{record.title || "Untitled"}</h1>
          <FeatureStateBadge
            state={record.feature_state}
            progress={record.feature_progress}
            jobId={record.job_id}
          />
        </div>
        {record.details ? (
          <p className="text-[var(--text-secondary)] mt-2 whitespace-pre-wrap">
            {record.details}
          </p>
        ) : null}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left column — cover + images */}
        <div className="lg:col-span-2 space-y-6">
          {record.cover_image ? (
            <div
              className="aspect-video bg-[var(--bg-tertiary)] rounded-lg overflow-hidden cursor-zoom-in"
              onClick={() => setLightboxSrc(record.cover_image!)}
            >
              <img
                src={record.cover_image}
                alt="cover"
                className="w-full h-full object-contain"
                onError={(e) => {
                  (e.currentTarget as HTMLImageElement).style.display = "none";
                }}
              />
            </div>
          ) : null}

          {record.images.length > 0 ? (
            <Card>
              <CardHeader>
                <CardTitle>Images ({record.images.length})</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2">
                  {record.images.map((src, i) => (
                    <button
                      key={i}
                      onClick={() => setLightboxSrc(src)}
                      className="aspect-video bg-[var(--bg-tertiary)] rounded overflow-hidden cursor-zoom-in"
                    >
                      <img
                        src={src}
                        alt={`image ${i + 1}`}
                        loading="lazy"
                        className="w-full h-full object-cover"
                        onError={(e) => {
                          (e.currentTarget as HTMLImageElement).style.display = "none";
                        }}
                      />
                    </button>
                  ))}
                </div>
              </CardContent>
            </Card>
          ) : null}
        </div>

        {/* Right column — metadata */}
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Details</CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="space-y-3 text-sm">
                <Row label="Job">
                  <span title={record.job_id}>{record.job_name}</span>
                </Row>
                <Row label="Index">
                  <span className="font-mono">{record.result_index}</span>
                </Row>
                {record.record_id ? (
                  <Row label="Record ID">
                    <span
                      className="font-mono"
                      title="Stable, content-derived identifier surfaced as record_id in /match/* responses (CLAUDE.md §15.4)"
                    >
                      {record.record_id}
                    </span>
                  </Row>
                ) : null}
                {record.id ? (
                  <Row label="Code">
                    <span className="font-mono">{record.id}</span>
                  </Row>
                ) : null}
                {record.date ? <Row label="Date">{record.date}</Row> : null}
                {record.url ? (
                  <Row label="URL">
                    <a
                      href={record.url}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="text-[var(--accent)] hover:underline break-all"
                    >
                      {record.url}
                    </a>
                  </Row>
                ) : null}
              </dl>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Performers ({record.performer_count})</CardTitle>
            </CardHeader>
            <CardContent>
              {record.performers.length === 0 ? (
                <p className="text-sm text-[var(--text-secondary)]">No performers</p>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {record.performers.map((p) => (
                    <Link key={p} to={`/performers/${encodeURIComponent(p.toLowerCase())}`}>
                      <Badge variant="default" className="hover:opacity-80">{p}</Badge>
                    </Link>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      <Lightbox isOpen={lightboxSrc !== null} onClose={() => setLightboxSrc(null)}>
        {lightboxSrc ? (
          <img src={lightboxSrc} alt="" className="max-w-[92vw] max-h-[88vh] object-contain rounded" />
        ) : null}
      </Lightbox>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-[var(--text-secondary)] shrink-0">{label}</dt>
      <dd className="text-right text-[var(--text-primary)] min-w-0 break-all">{children}</dd>
    </div>
  );
}
