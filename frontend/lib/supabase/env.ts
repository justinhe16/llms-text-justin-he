// Central place that reads the Supabase browser-visible environment
// variables. Both property accesses below MUST stay literal
// `process.env.NEXT_PUBLIC_X` reads — Next.js only inlines a literal
// `process.env.NEXT_PUBLIC_X` access into the client bundle at build time. A
// dynamic lookup such as `process.env[name]` is invisible to that build-time
// substitution and silently evaluates to `undefined` in the browser.

export type SupabaseEnv = { url: string; publishableKey: string };

/** The configuration, or `null` when either variable is unset. */
export function trySupabaseEnv(): SupabaseEnv | null {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const publishableKey = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;

  if (!url || !publishableKey) return null;
  return { url, publishableKey };
}

/** The names of whichever variables are unset. Names only — never values. */
export function missingSupabaseEnvNames(): string[] {
  const missing: string[] = [];
  if (!process.env.NEXT_PUBLIC_SUPABASE_URL) missing.push("NEXT_PUBLIC_SUPABASE_URL");
  if (!process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY) {
    missing.push("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY");
  }
  return missing;
}

// Throws when unconfigured, which is what the browser and server clients want:
// both variables are inlined at build time, so a missing one is a build-time
// misconfiguration and should fail loudly rather than degrade quietly.
// middleware.ts is the deliberate exception — see the comment there.
export function supabaseEnv(): SupabaseEnv {
  const env = trySupabaseEnv();
  if (env) return env;

  // Name the missing variable(s) only — never a value (ARCHITECTURE.md §9.4).
  throw new Error(
    `Missing required environment variable(s): ${missingSupabaseEnvNames().join(", ")}. ` +
      "See frontend/.env.example for how to set these for local development."
  );
}
