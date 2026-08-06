"use client";

import { GithubMark } from "@/components/auth/github-mark";
import { Button } from "@/components/ui/button";
import { useGithubSignIn } from "@/lib/auth/use-github-sign-in";

// The plain sign-in button. The landing page renders its own `ShimmerButton` instead of
// this one, but both drive the same `useGithubSignIn()` — the OAuth call, the sanitized
// `next` and the failure toast live there, not in either button.
export function GithubSignInButton() {
  const { signIn, isPending } = useGithubSignIn();

  return (
    <Button onClick={signIn} disabled={isPending}>
      <GithubMark className="size-4" />
      {isPending ? "Redirecting…" : "Continue with GitHub"}
    </Button>
  );
}
