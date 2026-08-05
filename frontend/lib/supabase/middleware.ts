// Refreshes the Supabase session on every request that passes through
// frontend/middleware.ts, and reports whether the caller is authenticated.
// Route-protection *policy* (which paths require auth, and what happens when
// they don't get it) deliberately does NOT live here — see
// frontend/middleware.ts, which is the only place that decides that.

import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

import { missingSupabaseEnvNames, trySupabaseEnv } from "@/lib/supabase/env";

// Middleware runs on every matched request, including public ones, so it is
// the one place that must NOT throw when Supabase is unconfigured or
// unreachable: a throw here turns a configuration problem or a provider
// outage into a site-wide 500 with a stack trace, landing page included.
// Both failure paths below degrade to "signed out" instead. That is
// fail-closed where it matters — frontend/middleware.ts bounces an
// unauthenticated visitor off a protected route — and fail-open only for
// pages that never required a session to begin with.
let hasWarnedAboutMissingEnv = false;

export async function updateSession(
  request: NextRequest
): Promise<{ response: NextResponse; isAuthenticated: boolean }> {
  const env = trySupabaseEnv();
  if (!env) {
    if (!hasWarnedAboutMissingEnv) {
      hasWarnedAboutMissingEnv = true;
      // Variable names only, never values (ARCHITECTURE.md §9.4). Logged once
      // per isolate: this is a deploy-wide condition, not a per-request one.
      console.error(
        `middleware: Supabase is not configured (missing ${missingSupabaseEnvNames().join(", ")}) — ` +
          "every request will be treated as signed out. See frontend/.env.example."
      );
    }
    return { response: NextResponse.next({ request }), isAuthenticated: false };
  }
  const { url, publishableKey } = env;

  let supabaseResponse = NextResponse.next({ request });

  const supabase = createServerClient(url, publishableKey, {
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      setAll(cookiesToSet) {
        cookiesToSet.forEach(({ name, value }) => request.cookies.set(name, value));
        // MUST be recreated here: NextResponse.next({ request }) captures the
        // request's headers/cookies at construction time, so the response
        // built above (before the cookies were refreshed) would still carry
        // the stale request.
        supabaseResponse = NextResponse.next({ request });
        cookiesToSet.forEach(({ name, value, options }) =>
          supabaseResponse.cookies.set(name, value, options)
        );
      },
    },
  });

  // Nothing may run between createServerClient() above and this call: any await in
  // between can make the refreshed session land after the response is built, which
  // logs users out at random.
  //
  // getClaims(), not getSession(): getSession() returns whatever is in the
  // cookie without revalidating it and must never gate access. getClaims()
  // verifies the JWT — locally when the project uses asymmetric signing keys,
  // otherwise via a call to the Auth server — and refreshes an
  // about-to-expire session first, which is what actually keeps the cookies
  // set above current.
  try {
    const { data } = await supabase.auth.getClaims();
    return { response: supabaseResponse, isAuthenticated: Boolean(data?.claims) };
  } catch (error) {
    // Reached when the Auth server can't be talked to at all (DNS, timeout,
    // an outage). Only the message is logged, never the error's payload, so
    // nothing that passed through a token can be printed here.
    console.error(
      "middleware: could not verify the Supabase session, treating this request as signed out —",
      error instanceof Error ? error.message : "unknown error"
    );
    return { response: supabaseResponse, isAuthenticated: false };
  }
}
