import { useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { api, MatchParams, ImageChannel, MatchMode } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/Card";
import { Badge } from "@/components/Badge";
import { Select } from "@/components/Select";
import { cn } from "@/lib/cn";

// ---- types --------------------------------------------------------------

interface CandidateLike {
  Title?: string | null;
  Code?: string | null;
  Studio?: { Name: string } | null;
  Image?: string | null;
  Performers?: { Name: string; Aliases?: string | null }[] | null;
  Date?: string | null;
  URL?: string | null;
  Details?: string | null;
  match_score?: number;
  record_id?: string | null;
  _debug?: unknown;
}

interface RunOutcome {
  status: number;
  retryAfter: number | null;
  data: unknown;
  request: object;
  endpoint: string;
}

// Sentinel for "use the bridge default" in Radix Select — Radix reserves
// the empty string for cleared selection, so Items can't use "".
const DEFAULT_SENTINEL = "__default__";

// ---- defaults -----------------------------------------------------------

const DEFAULT_PARAMS: MatchParams = {
  mode: "search",
  image_mode: null,
  threshold: null,
  limit: null,
  hash_algorithm: null,
  hash_size: null,
  sprite_sample_size: null,
  image_gamma: null,
  image_count_k: null,
  image_channels: null,
  image_min_contribution: null,
  image_bonus_per_extra: null,
  image_search_floor: null,
};

// ---- component ----------------------------------------------------------

export default function QueryPage() {
  const [tab, setTab] = useState<"fragment" | "name">("fragment");
  const [sceneId, setSceneId] = useState("");
  const [name, setName] = useState("");
  const [params, setParams] = useState<MatchParams>(DEFAULT_PARAMS);
  const [debug, setDebug] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [outcome, setOutcome] = useState<RunOutcome | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Force search mode on the Name tab (per OpenAPI contract).
  const effectiveMode: MatchMode = tab === "name" ? "search" : params.mode;

  const submit = async () => {
    setError(null);
    setOutcome(null);
    setSubmitting(true);
    try {
      const body: object = (() => {
        const base = { ...params, mode: effectiveMode };
        return tab === "fragment"
          ? { ...base, scene_id: sceneId.trim() }
          : { ...base, name: name.trim() };
      })();

      const endpoint = tab === "fragment" ? "/match/fragment" : "/match/name";
      const res = tab === "fragment"
        ? await api.matchByFragment(body as Parameters<typeof api.matchByFragment>[0], debug)
        : await api.matchByName(body as Parameters<typeof api.matchByName>[0], debug);

      setOutcome({
        status: res.status,
        retryAfter: res.retryAfter,
        data: res.data,
        request: body,
        endpoint,
      });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-bold">Query</h1>
        <p className="text-[var(--text-secondary)] mt-1">
          Run a match against the bridge. Operator tooling for debugging — does
          not modify any state.
        </p>
      </div>

      <Tabs.Root value={tab} onValueChange={(v) => setTab(v as "fragment" | "name")}>
        <Tabs.List className="flex gap-1 mb-4 border-b border-[var(--border)]">
          <TabTrigger value="fragment">Fragment (scene_id)</TabTrigger>
          <TabTrigger value="name">Scene Title (free text)</TabTrigger>
        </Tabs.List>

        <Tabs.Content value="fragment">
          <div className="space-y-3">
            <Field label="Scene ID" hint="Stash scene id (numeric).">
              <input
                type="text"
                value={sceneId}
                onChange={(e) => setSceneId(e.target.value)}
                placeholder="e.g. 17383"
                className={textInputCls}
              />
            </Field>
            <ModeRadio
              value={params.mode}
              onChange={(m) => setParams({ ...params, mode: m })}
            />
            <DebugCheckbox value={debug} onChange={setDebug} disabled={params.mode !== "search"} />
          </div>
        </Tabs.Content>

        <Tabs.Content value="name">
          <div className="space-y-3">
            <Field label="Name" hint="Free-text scene name. Forces search mode.">
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Beach Sunset"
                className={textInputCls}
              />
            </Field>
            <DebugCheckbox value={debug} onChange={setDebug} disabled={false} />
          </div>
        </Tabs.Content>

        <div className="mt-6">
          <AdvancedParamsBlock params={params} onChange={setParams} />
        </div>

        <div className="mt-6 flex items-center gap-3">
          <button
            onClick={submit}
            disabled={
              submitting ||
              (tab === "fragment" ? !sceneId.trim() : !name.trim())
            }
            className={cn(
              "px-4 py-2 rounded-md text-sm font-medium transition-colors",
              "bg-[var(--accent)] text-white hover:bg-[var(--accent-hover)]",
              "disabled:opacity-50 disabled:cursor-not-allowed",
            )}
          >
            {submitting ? "Submitting..." : "Submit"}
          </button>
          {error ? <span className="text-rose-500 text-sm">{error}</span> : null}
        </div>
      </Tabs.Root>

      {outcome ? (
        <div className="mt-8">
          <ResultPanel outcome={outcome} onRetry={submit} />
        </div>
      ) : null}
    </div>
  );
}

// ---- subcomponents ------------------------------------------------------

function TabTrigger({ value, children }: { value: string; children: React.ReactNode }) {
  return (
    <Tabs.Trigger
      value={value}
      className={cn(
        "px-4 py-2 text-sm rounded-t-md transition-colors",
        "data-[state=active]:bg-[var(--bg-secondary)] data-[state=active]:text-[var(--accent)]",
        "data-[state=active]:border-b-2 data-[state=active]:border-[var(--accent)]",
        "text-[var(--text-secondary)] hover:text-[var(--text-primary)]",
      )}
    >
      {children}
    </Tabs.Trigger>
  );
}

const textInputCls = cn(
  "w-full px-3 py-2 rounded-md text-sm bg-[var(--bg-tertiary)]",
  "border border-[var(--border)] text-[var(--text-primary)]",
  "focus:outline-none focus:ring-2 focus:ring-[var(--accent)] focus:border-transparent",
  "placeholder:text-[var(--text-secondary)]",
);

function Field({
  label, hint, children,
}: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="text-sm text-[var(--text-secondary)] mb-1">
        {label}
        {hint ? <span className="ml-2 text-xs opacity-70">{hint}</span> : null}
      </div>
      {children}
    </label>
  );
}

function ModeRadio({
  value, onChange,
}: { value: MatchMode; onChange: (m: MatchMode) => void }) {
  return (
    <Field label="Mode">
      <div className="flex gap-4">
        {(["scrape", "search"] as const).map((m) => (
          <label key={m} className="flex items-center gap-2 cursor-pointer">
            <input
              type="radio"
              name="mode"
              value={m}
              checked={value === m}
              onChange={() => onChange(m)}
            />
            <span className="text-sm">{m}</span>
          </label>
        ))}
      </div>
    </Field>
  );
}

function DebugCheckbox({
  value, onChange, disabled,
}: { value: boolean; onChange: (v: boolean) => void; disabled: boolean }) {
  return (
    <label className={cn("flex items-center gap-2", disabled && "opacity-50")}>
      <input
        type="checkbox"
        checked={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="text-sm">
        debug
        {disabled ? (
          <span className="ml-2 text-xs text-[var(--text-secondary)]">
            (search mode only)
          </span>
        ) : null}
      </span>
    </label>
  );
}

// ---- advanced params block ---------------------------------------------

function AdvancedParamsBlock({
  params, onChange,
}: { params: MatchParams; onChange: (p: MatchParams) => void }) {
  const [open, setOpen] = useState(false);
  const set = <K extends keyof MatchParams>(k: K, v: MatchParams[K]) =>
    onChange({ ...params, [k]: v });

  return (
    <details
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
      className="rounded-md border border-[var(--border)] bg-[var(--bg-secondary)]"
    >
      <summary className="cursor-pointer px-4 py-3 text-sm font-medium select-none">
        Advanced parameters {open ? "▾" : "▸"}
        <span className="ml-2 text-xs text-[var(--text-secondary)]">
          omitted fields use bridge defaults (CLAUDE.md §13.9)
        </span>
      </summary>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 p-4 border-t border-[var(--border)]">
        <Field label="image_mode">
          <Select
            value={params.image_mode ?? DEFAULT_SENTINEL}
            onValueChange={(v) =>
              set("image_mode", v === DEFAULT_SENTINEL ? null : (v as MatchParams["image_mode"]))
            }
            options={[
              { value: DEFAULT_SENTINEL, label: "(default)" },
              { value: "cover", label: "cover" },
              { value: "sprite", label: "sprite" },
              { value: "both", label: "both" },
            ]}
          />
        </Field>
        <Field label="hash_algorithm">
          <Select
            value={params.hash_algorithm ?? DEFAULT_SENTINEL}
            onValueChange={(v) =>
              set("hash_algorithm", v === DEFAULT_SENTINEL ? null : (v as MatchParams["hash_algorithm"]))
            }
            options={[
              { value: DEFAULT_SENTINEL, label: "(default)" },
              { value: "phash", label: "phash" },
              { value: "dhash", label: "dhash" },
              { value: "ahash", label: "ahash" },
              { value: "whash", label: "whash" },
            ]}
          />
        </Field>
        <NumberField label="threshold (0–1)" value={params.threshold} onChange={(v) => set("threshold", v)} step="0.01" min="0" max="1" />
        <NumberField label="limit (1–100)" value={params.limit} onChange={(v) => set("limit", v)} step="1" min="1" max="100" />
        <NumberField label="hash_size (8–32)" value={params.hash_size} onChange={(v) => set("hash_size", v)} step="1" min="8" max="32" />
        <NumberField label="sprite_sample_size (≥0)" value={params.sprite_sample_size} onChange={(v) => set("sprite_sample_size", v)} step="1" min="0" />
        <NumberField label="image_gamma (0.5–8)" value={params.image_gamma} onChange={(v) => set("image_gamma", v)} step="0.1" min="0.5" max="8" />
        <NumberField label="image_count_k (>0)" value={params.image_count_k} onChange={(v) => set("image_count_k", v)} step="0.05" min="0.01" />
        <NumberField label="image_min_contribution (0–1)" value={params.image_min_contribution} onChange={(v) => set("image_min_contribution", v)} step="0.01" min="0" max="1" />
        <NumberField label="image_bonus_per_extra (0–1)" value={params.image_bonus_per_extra} onChange={(v) => set("image_bonus_per_extra", v)} step="0.01" min="0" max="1" />
        <NumberField label="image_search_floor (0–1)" value={params.image_search_floor} onChange={(v) => set("image_search_floor", v)} step="0.01" min="0" max="1" />
        <Field label="image_channels (multiselect)">
          <ChannelMultiselect
            value={params.image_channels}
            onChange={(v) => set("image_channels", v)}
          />
        </Field>
      </div>
    </details>
  );
}

function NumberField({
  label, value, onChange, ...rest
}: {
  label: string;
  value: number | null | undefined;
  onChange: (v: number | null) => void;
  step?: string; min?: string; max?: string;
}) {
  return (
    <Field label={label}>
      <input
        type="number"
        value={value ?? ""}
        onChange={(e) => {
          const t = e.target.value;
          onChange(t === "" ? null : Number(t));
        }}
        className={textInputCls}
        placeholder="(default)"
        {...rest}
      />
    </Field>
  );
}

function ChannelMultiselect({
  value, onChange,
}: { value: ImageChannel[] | null | undefined; onChange: (v: ImageChannel[] | null) => void }) {
  const all: ImageChannel[] = ["phash", "color_hist", "tone", "embedding"];
  const set = new Set(value || []);
  const toggle = (ch: ImageChannel) => {
    if (set.has(ch)) set.delete(ch);
    else set.add(ch);
    onChange(set.size === 0 ? null : Array.from(set));
  };
  return (
    <div className="flex flex-wrap gap-2">
      {all.map((ch) => (
        <button
          key={ch}
          type="button"
          onClick={() => toggle(ch)}
          className={cn(
            "px-3 py-1 text-xs rounded-md border transition-colors",
            set.has(ch)
              ? "bg-[var(--accent)] text-white border-[var(--accent)]"
              : "bg-[var(--bg-tertiary)] border-[var(--border)] text-[var(--text-secondary)]",
          )}
        >
          {ch}
        </button>
      ))}
      {value === null || value === undefined ? (
        <span className="text-xs text-[var(--text-secondary)] self-center">
          (default)
        </span>
      ) : null}
    </div>
  );
}

// ---- result rendering ---------------------------------------------------

function ResultPanel({
  outcome, onRetry,
}: { outcome: RunOutcome; onRetry: () => void }) {
  if (outcome.status === 503) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Featurization in progress</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-[var(--text-secondary)] mb-3">
            The bridge returned 503 — the target job's feature index isn't
            ready yet (CLAUDE.md §14). Retry after about{" "}
            {outcome.retryAfter != null ? `${outcome.retryAfter} seconds` : "a moment"}.
          </p>
          <button
            onClick={onRetry}
            className="px-3 py-1.5 rounded-md text-sm bg-[var(--accent)] text-white hover:bg-[var(--accent-hover)]"
          >
            Retry now
          </button>
        </CardContent>
      </Card>
    );
  }

  if (outcome.status >= 400) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>HTTP {outcome.status}</CardTitle>
        </CardHeader>
        <CardContent>
          <pre className="text-xs whitespace-pre-wrap break-words">
            {JSON.stringify(outcome.data, null, 2)}
          </pre>
        </CardContent>
      </Card>
    );
  }

  // 200 OK. Determine shape: scrape returns object (or {}), search returns list.
  const data = outcome.data;
  const isList = Array.isArray(data);
  const candidates: CandidateLike[] = isList
    ? (data as CandidateLike[])
    : data && typeof data === "object" && Object.keys(data).length > 0
      ? [data as CandidateLike]
      : [];

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h2 className="text-lg font-semibold">
          Results
          <span className="ml-3 text-xs font-normal text-[var(--text-secondary)]">
            {outcome.endpoint} · HTTP {outcome.status}
          </span>
        </h2>
        <Badge variant="outline">
          {candidates.length} {isList ? "candidate" : "result"}{candidates.length !== 1 ? "s" : ""}
        </Badge>
      </div>

      {candidates.length === 0 ? (
        <div className="text-center py-8 text-[var(--text-secondary)] bg-[var(--bg-secondary)] rounded-lg border border-[var(--border)]">
          {isList ? "No candidates above zero." : "No definitive match (empty response)."}
        </div>
      ) : (
        <CandidateTable candidates={candidates} isSearch={isList} />
      )}

      <RawJsonPanel data={data} request={outcome.request} />
    </div>
  );
}

function CandidateTable({
  candidates, isSearch,
}: { candidates: CandidateLike[]; isSearch: boolean }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
      <table className="w-full text-sm">
        <thead className="bg-[var(--bg-secondary)] text-[var(--text-secondary)]">
          <tr className="text-left">
            <th className="px-3 py-2 w-12">#</th>
            <th className="px-3 py-2 w-24">Image</th>
            <th className="px-3 py-2">Title</th>
            <th className="px-3 py-2">ID</th>
            <th className="px-3 py-2">Studio</th>
            <th className="px-3 py-2">Performers</th>
            <th className="px-3 py-2 text-right">{isSearch ? "Score" : "Status"}</th>
          </tr>
        </thead>
        <tbody>
          {candidates.map((c, i) => (
            <CandidateRow key={i} index={i + 1} c={c} isSearch={isSearch} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CandidateRow({
  index, c, isSearch,
}: { index: number; c: CandidateLike; isSearch: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const hasDebug = !!c._debug;
  return (
    <>
      <tr className="border-t border-[var(--border)] align-top">
        <td className="px-3 py-2">{index}</td>
        <td className="px-3 py-2">
          {c.Image ? (
            <img src={c.Image} alt="" className="w-20 h-12 object-cover rounded" />
          ) : (
            <div className="w-20 h-12 bg-[var(--bg-tertiary)] rounded" />
          )}
        </td>
        <td className="px-3 py-2">
          <div className="font-medium">{c.Title || "Untitled"}</div>
          {c.Date ? (
            <div className="text-xs text-[var(--text-secondary)]">{c.Date}</div>
          ) : null}
          {c.URL ? (
            <a
              href={c.URL}
              target="_blank"
              rel="noreferrer noopener"
              className="text-xs text-[var(--accent)] hover:underline break-all"
            >
              {c.URL}
            </a>
          ) : null}
        </td>
        <td className="px-3 py-2 font-mono text-xs">
          {c.record_id ? (
            <div title="record_id (canonical, content-derived; CLAUDE.md §15.4)">
              {c.record_id}
            </div>
          ) : null}
          {c.Code ? (
            <div className="opacity-70" title="Code (= scraped data.id; optional)">
              Code: {c.Code}
            </div>
          ) : null}
          {!c.record_id && !c.Code ? "-" : null}
        </td>
        <td className="px-3 py-2 text-xs">{c.Studio?.Name || "-"}</td>
        <td className="px-3 py-2">
          <div className="flex flex-wrap gap-1">
            {(c.Performers || []).map((p, i) => (
              <Badge key={i} variant="secondary">{p.Name}</Badge>
            ))}
            {(!c.Performers || c.Performers.length === 0) ? (
              <span className="text-xs text-[var(--text-secondary)]">-</span>
            ) : null}
          </div>
        </td>
        <td className="px-3 py-2 text-right whitespace-nowrap">
          {isSearch ? (
            typeof c.match_score === "number" ? c.match_score.toFixed(4) : "-"
          ) : (
            <Badge variant="success">definitive</Badge>
          )}
          {hasDebug ? (
            <button
              onClick={() => setExpanded(!expanded)}
              className="ml-2 text-xs text-[var(--accent)] hover:underline"
            >
              {expanded ? "hide" : "debug"}
            </button>
          ) : null}
        </td>
      </tr>
      {expanded && hasDebug ? (
        <tr className="border-t border-[var(--border)]">
          <td colSpan={7} className="px-3 py-2 bg-[var(--bg-primary)]">
            <pre className="text-xs whitespace-pre-wrap break-words font-mono max-h-96 overflow-auto">
              {JSON.stringify(c._debug, null, 2)}
            </pre>
          </td>
        </tr>
      ) : null}
    </>
  );
}

// navigator.clipboard is undefined in non-secure contexts (HTTP on a
// non-localhost origin, which is how this viewer is typically reached over
// a LAN). Fall back to a hidden-textarea + document.execCommand round-trip
// so the button works regardless of origin.
async function copyText(text: string): Promise<boolean> {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall through to execCommand
    }
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "0";
    ta.style.left = "0";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

function RawJsonPanel({ data, request }: { data: unknown; request: object }) {
  const [copiedKey, setCopiedKey] = useState<"req" | "res" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reqText = JSON.stringify(request, null, 2);
  const resText = JSON.stringify(data, null, 2);
  const copy = async (key: "req" | "res", text: string) => {
    setError(null);
    const ok = await copyText(text);
    if (ok) {
      setCopiedKey(key);
      setTimeout(() => setCopiedKey((k) => (k === key ? null : k)), 1500);
    } else {
      setError("copy failed");
      setTimeout(() => setError(null), 2500);
    }
  };
  return (
    <details className="rounded-md border border-[var(--border)] bg-[var(--bg-secondary)]">
      <summary className="cursor-pointer px-4 py-2 text-sm font-medium select-none">
        Raw JSON
        {error ? (
          <span className="ml-2 text-xs text-rose-500">{error}</span>
        ) : null}
      </summary>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 p-4 border-t border-[var(--border)]">
        <div>
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs text-[var(--text-secondary)]">Request</span>
            <button
              onClick={() => copy("req", reqText)}
              className="text-xs px-2 py-1 rounded bg-[var(--bg-tertiary)] hover:bg-[var(--border)]"
            >
              {copiedKey === "req" ? "copied" : "copy"}
            </button>
          </div>
          <pre className="text-xs whitespace-pre-wrap break-words font-mono max-h-72 overflow-auto bg-[var(--bg-primary)] p-3 rounded">
            {reqText}
          </pre>
        </div>
        <div>
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs text-[var(--text-secondary)]">Response</span>
            <button
              onClick={() => copy("res", resText)}
              className="text-xs px-2 py-1 rounded bg-[var(--bg-tertiary)] hover:bg-[var(--border)]"
            >
              {copiedKey === "res" ? "copied" : "copy"}
            </button>
          </div>
          <pre className="text-xs whitespace-pre-wrap break-words font-mono max-h-72 overflow-auto bg-[var(--bg-primary)] p-3 rounded">
            {resText}
          </pre>
        </div>
      </div>
    </details>
  );
}
