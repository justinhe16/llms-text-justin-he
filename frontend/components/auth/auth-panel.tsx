"use client";

import { DevPasswordSignIn } from "@/components/auth/dev-password-sign-in";
import { GithubSignInButton } from "@/components/auth/github-sign-in-button";
import { UserMenu } from "@/components/auth/user-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { useUser } from "@/lib/auth/use-user";

export function AuthPanel() {
  const { user, isLoading } = useUser();

  if (isLoading) {
    return <Skeleton className="h-8 w-32 rounded-full" />;
  }

  if (user) {
    return <UserMenu />;
  }

  return (
    <div className="flex flex-col items-center gap-3">
      <GithubSignInButton />
      <DevPasswordSignIn />
    </div>
  );
}
