"use client";

import { useState } from "react";
import { useSearchParams } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { safeNextPath } from "@/lib/auth/next-path";
import { createClient } from "@/lib/supabase/client";

// lucide-react@1.28 does not ship a `Github` icon (verified against
// node_modules/lucide-react/dist/esm/icons — there is no github.* file
// there), so the mark is inlined instead of guessed at.
function GithubMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M12 .5C5.73.5.5 5.73.5 12c0 5.09 3.29 9.4 7.86 10.93.57.1.79-.25.79-.55 0-.27-.01-1.17-.02-2.12-3.2.7-3.88-1.36-3.88-1.36-.52-1.34-1.28-1.7-1.28-1.7-1.04-.72.08-.7.08-.7 1.16.08 1.77 1.19 1.77 1.19 1.03 1.77 2.7 1.26 3.36.96.1-.75.4-1.26.73-1.55-2.56-.29-5.25-1.28-5.25-5.71 0-1.26.45-2.29 1.19-3.1-.12-.29-.52-1.47.11-3.06 0 0 .97-.31 3.18 1.18a11.06 11.06 0 0 1 2.9-.39c.98 0 1.97.13 2.9.39 2.2-1.49 3.17-1.18 3.17-1.18.64 1.59.24 2.77.12 3.06.74.81 1.19 1.84 1.19 3.1 0 4.44-2.7 5.42-5.27 5.7.41.36.78 1.07.78 2.15 0 1.55-.01 2.8-.01 3.18 0 .3.21.66.8.55A11.5 11.5 0 0 0 23.5 12C23.5 5.73 18.27.5 12 .5Z" />
    </svg>
  );
}

export function GithubSignInButton() {
  const searchParams = useSearchParams();
  const [isPending, setIsPending] = useState(false);

  async function handleClick() {
    setIsPending(true);

    const next = safeNextPath(searchParams.get("next"));
    const supabase = createClient();
    // Origin computed from window.location.origin, never hardcoded, so this
    // works unchanged across local dev, previews, and production.
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "github",
      options: {
        redirectTo: `${window.location.origin}/auth/callback?next=${encodeURIComponent(next)}`,
      },
    });

    if (error) {
      setIsPending(false);
      toast.error("Couldn't start GitHub sign-in. Try again.");
      return;
    }
    // On success the browser is about to navigate to GitHub, so there is no
    // "done" state to render here — isPending stays true until that happens.
  }

  return (
    <Button onClick={handleClick} disabled={isPending}>
      <GithubMark className="size-4" />
      {isPending ? "Redirecting…" : "Continue with GitHub"}
    </Button>
  );
}
