"use client";

import { useState } from "react";
import { useSearchParams } from "next/navigation";
import { toast } from "sonner";

import { safeNextPath } from "@/lib/auth/next-path";
import { createClient } from "@/lib/supabase/client";

/**
 * Starting the GitHub OAuth flow, as behaviour rather than as a button.
 *
 * It was a private function inside a sign-in *button* until the landing page needed the
 * same action from two controls at once: the primary `ShimmerButton`, and the URL field,
 * which is disabled while signed out and starts sign-in when clicked rather than dead-ending.
 * Living here means the redirect URL, the `next` sanitization and the failure toast are
 * written once and shared by both — ARCHITECTURE.md §8.4's "a feature's non-visual logic
 * lives in `lib/`, not in its components" — and §8.2's "`lib/supabase/client.ts` … the only
 * places that construct a Supabase client" stays true with one caller here rather than one
 * per affordance.
 *
 * `isPending` never returns to `false` on success, deliberately: the browser is already
 * navigating to GitHub, so there is no "done" state to render — only a redirect that has
 * or has not started yet.
 */
export function useGithubSignIn(): {
  signIn: () => Promise<void>;
  isPending: boolean;
} {
  const searchParams = useSearchParams();
  const [isPending, setIsPending] = useState(false);

  async function signIn(): Promise<void> {
    setIsPending(true);

    // Where to land after the callback. Sanitized, because middleware.ts writes this param
    // on every redirect it makes and a crafted link could otherwise point it off-origin.
    const next = safeNextPath(searchParams.get("next"));
    const supabase = createClient();
    // Origin computed from window.location.origin, never hardcoded, so this works
    // unchanged across local dev, previews, and production.
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "github",
      options: {
        redirectTo: `${window.location.origin}/auth/callback?next=${encodeURIComponent(next)}`,
      },
    });

    if (error) {
      setIsPending(false);
      toast.error("Couldn't start GitHub sign-in. Try again.");
    }
  }

  return { signIn, isPending };
}
