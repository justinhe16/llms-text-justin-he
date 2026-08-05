import { NextResponse, type NextRequest } from "next/server";

import { safeNextPath } from "@/lib/auth/next-path";
import { createClient } from "@/lib/supabase/server";

// GitHub OAuth redirects the browser here with either `code` (success) or
// `error`/`error_description` (denied or failed). `error_description` is
// deliberately never read: it is untrusted text from an external redirect
// and must never be rendered to the user — components/auth/auth-error-toast.tsx
// maps the small set of reasons below to fixed, readable copy instead.
export async function GET(request: NextRequest) {
  const { searchParams, origin } = request.nextUrl;
  const code = searchParams.get("code");
  const error = searchParams.get("error");
  const next = safeNextPath(searchParams.get("next"));

  // Computed the way Supabase's own reference implementation does, so it
  // survives Vercel's load balancer: in production `origin` reflects
  // whatever internal host the request reached Next.js on, which is not
  // necessarily the public host the browser used.
  const forwardedHost = request.headers.get("x-forwarded-host");
  const base =
    process.env.NODE_ENV === "development"
      ? origin
      : forwardedHost
        ? `https://${forwardedHost}`
        : origin;

  if (error) {
    const reason = error === "access_denied" ? "denied" : "provider";
    return NextResponse.redirect(`${base}/?auth_error=${reason}`);
  }

  if (!code) {
    return NextResponse.redirect(`${base}/?auth_error=missing_code`);
  }

  const supabase = await createClient();
  const { error: exchangeError } = await supabase.auth.exchangeCodeForSession(code);
  if (exchangeError) {
    return NextResponse.redirect(`${base}/?auth_error=exchange`);
  }

  return NextResponse.redirect(`${base}${next}`);
}
