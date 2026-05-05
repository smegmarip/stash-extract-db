import { Express, Request, Response } from "express";
import { createProxyMiddleware, Options } from "http-proxy-middleware";

/**
 * Routing model
 *
 *   browser ───► viewer (Express, this file) ───► bridge or extractor
 *
 *   /health                  → respond locally (no upstream dep)
 *   /api/health              → bridge /health
 *   /api/admin/*             → bridge /api/admin/*
 *   /api/featurization/*     → bridge /api/featurization/*
 *   /api/extraction/*        → bridge /api/extraction/*
 *   /match/*                 → bridge /match/*
 *   /api/asset/{job_id}/...  → extractor /api/asset/{job_id}/...
 *
 * Stash auth env vars (STASH_API_KEY, STASH_SESSION_COOKIE) are accepted but
 * unused in v1 — the viewer doesn't call Stash directly. Documented in
 * docs/VIEWER_PLAN.md §5 for forward compatibility.
 */

const BRIDGE_URL = process.env.BRIDGE_URL || "http://localhost:13000";
const EXTRACTOR_URL = process.env.EXTRACTOR_URL || "http://localhost:12000";

function proxy(target: string, opts: Partial<Options> = {}) {
  return createProxyMiddleware({
    target,
    changeOrigin: true,
    xfwd: true,
    timeout: 60_000,
    proxyTimeout: 60_000,
    ...opts,
  });
}

export function setupRoutes(app: Express): void {
  // Local health — does not touch upstream so the viewer can report ready
  // even when the bridge is restarting.
  app.get("/health", (_req: Request, res: Response) => {
    res.json({
      status: "ok",
      timestamp: new Date().toISOString(),
      bridge_url: BRIDGE_URL,
      extractor_url: EXTRACTOR_URL,
    });
  });

  // Bridge passthrough. Express's `app.use(<mount>, ...)` strips the mount
  // prefix before the proxy runs, so we restore it via pathRewrite.
  app.use("/api/admin", proxy(BRIDGE_URL, {
    pathRewrite: (path) => `/api/admin${path}`,
  }));
  app.use("/api/featurization", proxy(BRIDGE_URL, {
    pathRewrite: (path) => `/api/featurization${path}`,
  }));
  app.use("/api/extraction", proxy(BRIDGE_URL, {
    pathRewrite: (path) => `/api/extraction${path}`,
  }));
  app.use("/api/health", proxy(BRIDGE_URL, {
    pathRewrite: () => "/health",
  }));

  // Extractor asset proxy. Bridge-relative URLs returned by /api/admin point
  // here, so the browser never reaches the extractor directly.
  app.use("/api/asset", proxy(EXTRACTOR_URL, {
    pathRewrite: (path) => `/api/asset${path}`,
  }));

  // Match endpoints (POST). Stream payload to bridge as-is.
  app.use("/match", proxy(BRIDGE_URL, {
    pathRewrite: (path) => `/match${path}`,
  }));
}
