// Named wrapper around `GET /health`, mirroring lib/api/websites.ts's pattern for a
// second, much smaller feature. Kept mostly to prove that `api.get` (lib/api/fetcher.ts)
// generalizes cleanly to a path with no parameters, no query, and no request body at all —
// every field it forwards comes straight from `paths["/health"]["get"]`.

import type { components } from "./schema";
import { api } from "./fetcher";

export type HealthStatus = components["schemas"]["HealthResponse"];

/** `GET /health`. Always resolves with a `200` — see `HealthResponse`'s own docstring in
 * backend/app/api/routers/health.py for why dependency trouble is reported in the body
 * rather than as a non-2xx status this client would otherwise have to catch: restarting a
 * healthy machine does nothing to fix a Postgres or Redis outage.
 *
 * The body carries one field per checked dependency — `db` and, since PER-157 added the ARQ
 * pool, `redis` — plus an overall `status` that is `"ok"` only when every one of them is.
 * A caller that reads only `status` still gets an accurate signal without having to know
 * which dependencies exist, which is what keeps this client from needing a change every
 * time the backend grows another one. */
export function getHealth(): Promise<HealthStatus> {
  return api.get("/health");
}
