"use client";

import { useEffect, useState } from "react";
import type { AuthUser as SupabaseUser } from "@supabase/supabase-js";

import { createClient } from "@/lib/supabase/client";

export type AuthUser = {
  id: string;
  email: string | null;
  handle: string | null;
  avatarUrl: string | null;
  displayName: string;
};

function readStringField(metadata: Record<string, unknown>, key: string): string | null {
  const value = metadata[key];
  return typeof value === "string" ? value : null;
}

function mapUser(user: SupabaseUser): AuthUser {
  const metadata: Record<string, unknown> = user.user_metadata;
  const handle =
    readStringField(metadata, "user_name") ?? readStringField(metadata, "preferred_username");
  const avatarUrl = readStringField(metadata, "avatar_url");
  const fullName = readStringField(metadata, "full_name") ?? readStringField(metadata, "name");
  const email = user.email ?? null;

  return {
    id: user.id,
    email,
    handle,
    avatarUrl,
    displayName: handle ?? fullName ?? email ?? "Signed in",
  };
}

function sameUser(previous: AuthUser | null, next: AuthUser): boolean {
  if (!previous) return false;
  return (
    previous.id === next.id &&
    previous.email === next.email &&
    previous.handle === next.handle &&
    previous.avatarUrl === next.avatarUrl
  );
}

// Display only — this is never an authorization check. Route access is
// enforced server-side in frontend/middleware.ts; this hook exists purely to
// decide what to render (signed-in vs. signed-out UI).
export function useUser(): { user: AuthUser | null; isLoading: boolean } {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const supabase = createClient();

    // No getUser()/getSession() call on mount: onAuthStateChange emits
    // INITIAL_SESSION synchronously on subscribe, carrying the session
    // already restored from the cookie — that alone gives the initial state
    // with no extra network round trip.
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      const nextUser = session?.user ?? null;

      // Refetch-storm guard: onAuthStateChange also fires TOKEN_REFRESHED on
      // every silent token refresh, with a brand-new session/user object
      // even when nothing about the mapped identity changed. The functional
      // updater form lets this compare against the previous value and return
      // it unchanged (same object identity) whenever the mapped fields
      // match, so consumers of useUser() don't re-render on a timer for no
      // visible change.
      setUser((previous) => {
        if (!nextUser) return null;
        const mapped = mapUser(nextUser);
        return sameUser(previous, mapped) ? previous : mapped;
      });
      setIsLoading(false);
    });

    return () => {
      subscription.unsubscribe();
    };
  }, []);

  return { user, isLoading };
}
