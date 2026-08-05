"use server";

import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";

export async function signOut(): Promise<void> {
  const supabase = await createClient();

  // Errors here are ignored — in particular "Auth session missing!", which
  // happens when the browser client (components/auth/user-menu.tsx) already
  // cleared its own session before this ran. Either way this call still
  // clears the cookies Next.js sees, so signing out and redirecting below is
  // correct regardless of what it returns.
  await supabase.auth.signOut();

  // redirect() throws a special Next.js control-flow exception by design —
  // it must never be wrapped in a try/catch, or the throw would be
  // swallowed and the redirect would silently not happen.
  redirect("/");
}
