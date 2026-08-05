"use client";

import { useState, type FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { safeNextPath } from "@/lib/auth/next-path";
import { createClient } from "@/lib/supabase/client";

const LOCAL_DEV_EMAIL = "dev@llms-text.test";
const LOCAL_DEV_PASSWORD = "devpassword123";

// GitHub OAuth is disabled in supabase/config.toml for local development
// ([auth.external.github] enabled = false — see README.md "Local test
// user"), so this email/password form, signing in as the fixed user seeded
// by supabase/seed.sql, is the only way to exercise the real
// cookie/session/middleware path on a laptop. `devpassword123` unlocks a
// Postgres container running on localhost only — a documented local
// convenience, not a secret.
//
// The env check is the first line of the component body specifically so
// Next dead-code-eliminates it from every deployed build: Vercel preview and
// production builds both set NODE_ENV=production, so this literal
// comparison lets the bundler prove the branch is always taken and drop
// everything below it. Hooks can't follow an early return in the same
// function (react-hooks/rules-of-hooks), so the guard lives in this outer
// component and the actual form — with its hooks — lives in the inner one,
// which is simply never rendered outside development.
export function DevPasswordSignIn() {
  if (process.env.NODE_ENV === "production") return null;
  return <DevPasswordSignInForm />;
}

function DevPasswordSignInForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [email, setEmail] = useState(LOCAL_DEV_EMAIL);
  const [password, setPassword] = useState(LOCAL_DEV_PASSWORD);
  const [isPending, setIsPending] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setIsPending(true);

    const supabase = createClient();
    const { error } = await supabase.auth.signInWithPassword({ email, password });

    if (error) {
      setIsPending(false);
      toast.error(error.message);
      return;
    }

    const next = safeNextPath(searchParams.get("next"));
    router.push(next);
    // Middleware runs server-side and only sees the cookies
    // signInWithPassword just set once Next re-requests. router.refresh()
    // forces that re-request so middleware re-runs against the new session.
    router.refresh();
  }

  return (
    <form onSubmit={handleSubmit} className="flex w-full max-w-xs flex-col gap-2">
      <p className="text-xs text-muted-foreground">
        Local development only — signs in as the seeded test user.
      </p>
      <Input
        type="email"
        value={email}
        onChange={(event) => setEmail(event.target.value)}
        placeholder="Email"
        aria-label="Email"
        required
      />
      <Input
        type="password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        placeholder="Password"
        aria-label="Password"
        required
      />
      <Button type="submit" variant="outline" disabled={isPending}>
        {isPending ? "Signing in…" : "Sign in (dev)"}
      </Button>
    </form>
  );
}
