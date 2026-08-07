"use client";

import Image from "next/image";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { signOut } from "@/lib/auth/actions";
import { initials } from "@/lib/auth/initials";
import { useUser } from "@/lib/auth/use-user";
import { createClient } from "@/lib/supabase/client";

async function handleSignOut() {
  // Clear the browser client's own session first: this emits SIGNED_OUT
  // through onAuthStateChange, so useUser() (and this menu) updates
  // immediately without waiting on a navigation. Then call the server
  // action, which clears the cookies Next.js reads on the server and
  // redirects.
  await createClient().auth.signOut();
  await signOut();
}

export function UserMenu() {
  const { user, isLoading } = useUser();

  if (isLoading) {
    return <Skeleton className="size-8 rounded-full" />;
  }

  if (!user) {
    return null;
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger className="flex items-center gap-2 rounded-full border border-border bg-card px-2 py-1 text-sm text-foreground">
        {user.avatarUrl ? (
          <Image
            src={user.avatarUrl}
            alt=""
            width={24}
            height={24}
            className="size-6 rounded-full"
          />
        ) : (
          <span className="flex size-6 items-center justify-center rounded-full bg-muted text-xs font-medium text-muted-foreground">
            {initials(user.displayName)}
          </span>
        )}
        <span className="max-w-32 truncate">{user.displayName}</span>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {user.email ? (
          <DropdownMenuLabel className="font-normal text-muted-foreground">
            {user.email}
          </DropdownMenuLabel>
        ) : null}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={handleSignOut}>Sign out</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
