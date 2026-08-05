"use client";

import { useEffect, useRef } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { toast } from "sonner";

const AUTH_ERROR_MESSAGES: Record<string, string> = {
  denied: "GitHub sign-in was cancelled. Nothing was changed.",
  missing_code: "That sign-in link is incomplete. Try signing in again.",
  exchange: "We couldn't finish signing you in. Try again.",
};

const DEFAULT_MESSAGE = "Sign-in failed. Try again.";

// Reads `?auth_error=` set by app/auth/callback/route.ts and turns it into a
// toast. Never renders the raw provider error string or a stack trace — only
// this small, fixed set of readable messages.
export function AuthErrorToast() {
  const searchParams = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();
  const authError = searchParams.get("auth_error");
  // Guards against toasting the same value twice: React's strict mode
  // double-invokes effects in development, and this ref makes the second
  // invocation for a given value a no-op.
  const shownFor = useRef<string | null>(null);

  useEffect(() => {
    if (!authError || shownFor.current === authError) return;
    shownFor.current = authError;

    toast.error(AUTH_ERROR_MESSAGES[authError] ?? DEFAULT_MESSAGE);
    // Strips the param so a refresh of this page doesn't re-toast.
    router.replace(pathname);
  }, [authError, pathname, router]);

  return null;
}
