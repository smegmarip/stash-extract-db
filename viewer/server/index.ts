import dotenv from "dotenv";
import express from "express";
import cors from "cors";
import path from "path";
import { fileURLToPath } from "url";
import { setupRoutes } from "./routes.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Load .env from viewer/ root (one level up from server/) so that local dev
// reads viewer/.env. Production deployments inject env directly.
dotenv.config({ path: path.resolve(__dirname, "../../.env") });

const app = express();
const PORT = parseInt(process.env.PORT || "3100", 10);

app.use(cors());
// Intentionally no body parser. The viewer's only handler (/health) is GET;
// every POST hits a proxy. express.json would consume the request stream,
// leaving http-proxy-middleware nothing to forward — the bridge would hang
// and the browser would see ERR_EMPTY_RESPONSE.

// Lightweight request logging — one line per request.
app.use((req, _res, next) => {
  console.log(`[${new Date().toISOString()}] ${req.method} ${req.path}`);
  next();
});

setupRoutes(app);

// Serve the built SPA in production.
if (process.env.NODE_ENV === "production") {
  const clientPath = path.join(__dirname, "../client");
  app.use(express.static(clientPath));
  // SPA fallback for any non-API route.
  app.get(/^(?!\/(api|match|health)).*/, (_req, res) => {
    res.sendFile(path.join(clientPath, "index.html"));
  });
}

const server = app.listen(PORT, () => {
  console.log(`[viewer] listening on http://0.0.0.0:${PORT}`);
  console.log(`[viewer] BRIDGE_URL=${process.env.BRIDGE_URL || "http://localhost:13000"}`);
  console.log(`[viewer] EXTRACTOR_URL=${process.env.EXTRACTOR_URL || "(unset)"}`);
});

process.on("SIGTERM", () => {
  console.log("[viewer] SIGTERM received, closing server");
  server.close(() => process.exit(0));
});
