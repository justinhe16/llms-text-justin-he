// Named wrapper around `GET /health`, mirroring lib/api/websites.ts's pattern for a
// second, much smaller feature. Kept mostly to prove that `api.get` (lib/api/fetcher.ts)
// generalizes cleanly to a path with no parameters, no query, and no request body at all —
// every field it forwards comes straight from `paths["/health"]["get"]`.

import type { components } from "./schema";
import { api } from "./fetcher";

export type HealthStatus = components["schemas"]["HealthResponse"];

/** `GET /health`. Always resolves with a `200` — see `HealthResponse`'s own docstring in
 * backend/app/api/routers/health.py for why database trouble is reported in the body's
 * `db` field rather than as a non-2xx status this client would otherwise have to catch. */
export function getHealth(): Promise<HealthStatus> {
  return api.get("/health");
}
