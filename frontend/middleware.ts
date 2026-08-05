import { NextResponse, type NextRequest } from "next/server";

import { updateSession } from "@/lib/supabase/middleware";

// Route-protection policy lives HERE, not in lib/supabase/middleware.ts, so
// the session-refresh helper stays reusable and this list stays the one
// place that decides what's gated. /crawls and /crawls/[id] don't exist yet
// (later ticket) — a redirect for a not-yet-existing route is expected until
// then, and this keeps working unchanged once they land.
const PROTECTED_PREFIXES = ["/crawls"] as const;

function isProtectedPath(pathname: string): boolean {
  return PROTECTED_PREFIXES.some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`)
  );
}

export async function middleware(request: NextRequest) {
  const { response, isAuthenticated } = await updateSession(request);

  if (isAuthenticated || !isProtectedPath(request.nextUrl.pathname)) {
    return response;
  }

  const redirectUrl = request.nextUrl.clone();
  redirectUrl.pathname = "/";
  redirectUrl.search = "";
  redirectUrl.searchParams.set("next", `${request.nextUrl.pathname}${request.nextUrl.search}`);

  const redirectResponse = NextResponse.redirect(redirectUrl);

  // Copy every cookie updateSession() set onto the redirect response. Without
  // this, a session refreshed by updateSession() during this same request
  // (e.g. a token nearing expiry) lives only on `response`, which is about to
  // be discarded in favor of `redirectResponse` — the refreshed session would
  // be thrown away instead of reaching the browser.
  response.cookies.getAll().forEach((cookie) => redirectResponse.cookies.set(cookie));

  return redirectResponse;
}

export const config = {
  // The Supabase-docs default matcher: everything except static assets and
  // common image files, so middleware doesn't do session-refresh work on
  // requests that can't carry a session anyway.
  //
  // `/` and `/docs` are deliberately public (no auth required) and are NOT in
  // PROTECTED_PREFIXES — but the matcher still covers them, on purpose, so
  // updateSession() runs there too and keeps a signed-in visitor's session
  // fresh even on pages that don't require one.
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)",
  ],
};
