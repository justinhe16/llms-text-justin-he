// Browser Supabase client.
//
// This client is AUTH-ONLY: never call `.from(...)` or `.storage.from(...)`
// on the object it returns. ARCHITECTURE.md §1 — the frontend never talks to
// Postgres, and browser-side Storage access is out of scope for the same
// reason. Application data reaches the browser only through the FastAPI
// proxy under app/api/[...path]/.

import { createBrowserClient } from "@supabase/ssr";

import { supabaseEnv } from "@/lib/supabase/env";

export function createClient() {
  const { url, publishableKey } = supabaseEnv();
  return createBrowserClient(url, publishableKey);
}
