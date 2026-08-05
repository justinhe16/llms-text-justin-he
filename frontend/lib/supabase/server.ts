// Server-side Supabase client, for use in Server Components, Route Handlers,
// and Server Actions. Reads and writes the session cookie through Next's
// `cookies()` API, which is async as of Next 15.

import { cookies } from "next/headers";
import { createServerClient } from "@supabase/ssr";

import { supabaseEnv } from "@/lib/supabase/env";

export async function createClient() {
  const cookieStore = await cookies();
  const { url, publishableKey } = supabaseEnv();

  return createServerClient(url, publishableKey, {
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      setAll(cookiesToSet) {
        try {
          cookiesToSet.forEach(({ name, value, options }) =>
            cookieStore.set(name, value, options)
          );
        } catch {
          // Called from a Server Component, which cannot write cookies. Safe to
          // swallow: middleware.ts refreshes the session on every request.
        }
      },
    },
  });
}
