"use client";

import Link from "next/link";

import { GithubMark } from "@/components/auth/github-mark";
import { SiteUrlField } from "@/components/landing/site-url-field";
import { BlurFade } from "@/components/magicui/blur-fade";
import { ShimmerButton } from "@/components/magicui/shimmer-button";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useGithubSignIn } from "@/lib/auth/use-github-sign-in";
import { useUser } from "@/lib/auth/use-user";
import { useAddSite } from "@/lib/landing/use-add-site";

/**
 * Everything on the landing page that reacts to who you are: the URL field and the two
 * buttons under it.
 *
 * One client island rather than three, because all of it turns on the same two facts —
 * whether there is a session, and whether a submit is in flight — and splitting it would
 * mean three `useUser()` subscriptions agreeing with each other by luck.
 *
 * `useUser()` here is a *display* decision, never an authorization one (ARCHITECTURE.md
 * §8.2). Nothing on this page is gated by it: `/crawls` is protected server-side in
 * `frontend/middleware.ts`, and the two endpoints the field calls are protected by the
 * backend. What this decides is whether the field is usable and which primary button to
 * render.
 */
export function LandingForm() {
  const { user, isLoading: isAuthLoading } = useUser();
  const { signIn, isPending: isSigningIn } = useGithubSignIn();
  const { submit, retry, clearError, error, isBusy } = useAddSite();

  const isSignedIn = user !== null;

  return (
    <>
      <BlurFade delay={0.18} className="w-full">
        <SiteUrlField
          isSignedIn={isSignedIn}
          isAuthLoading={isAuthLoading}
          isBusy={isBusy}
          error={error}
          onSubmit={submit}
          onClearError={clearError}
          onRetry={retry}
          onSignIn={signIn}
        />
      </BlurFade>

      <BlurFade delay={0.26}>
        <div className="flex flex-wrap items-center justify-center gap-3">
          {isAuthLoading ? (
            // Matches the primary button's box so the row does not jump when the session
            // resolves. The same skeleton-while-unknown the rest of the app uses.
            <Skeleton className="h-11 w-44 rounded-full" />
          ) : isSignedIn ? (
            <ShimmerButton asChild className="h-11 px-5 text-sm font-medium">
              <Link href="/crawls">Go to crawls</Link>
            </ShimmerButton>
          ) : (
            <ShimmerButton
              onClick={signIn}
              disabled={isSigningIn}
              className="h-11 gap-2 px-5 text-sm font-medium"
            >
              <GithubMark className="size-4" />
              {isSigningIn ? "Redirecting…" : "Sign in with GitHub"}
            </ShimmerButton>
          )}

          <Button asChild variant="ghost" className="h-11 rounded-full px-5">
            <Link href="/docs">Documentation</Link>
          </Button>
        </div>
      </BlurFade>
    </>
  );
}
