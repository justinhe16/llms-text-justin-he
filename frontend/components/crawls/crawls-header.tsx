import Link from "next/link";
import { Plus } from "lucide-react";

import { UserMenu } from "@/components/auth/user-menu";
import { Button } from "@/components/ui/button";

/**
 * The application's only chrome.
 *
 * Deliberately not in `app/layout.tsx`: the landing page has no header at all (see the
 * comment in that file), and this is the first screen that needs one. A header in the root
 * layout would have to be conditionally hidden on `/`, which is how a layout ends up knowing
 * about routes.
 *
 * "Add site" is a link to the home page, not a dialog. Creating a website belongs to the
 * landing page's URL input — one place that owns it, doing the normalization and the
 * already-exists handling — and a second entry point here would either duplicate that or
 * quietly diverge from it.
 *
 * A server component: it renders one client island (`UserMenu`, which needs the browser's
 * Supabase session to know who you are) and is otherwise two links.
 */
export function CrawlsHeader() {
  return (
    <header className="sticky top-0 z-30 border-b border-border bg-background/85 backdrop-blur-sm">
      <div className="mx-auto flex h-14 w-full max-w-6xl items-center gap-3 px-4 sm:px-6">
        <Link
          href="/"
          className="rounded-md font-mono text-sm font-medium tracking-tight text-foreground outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
        >
          llms-text
        </Link>

        <div className="ml-auto flex items-center gap-2">
          <Button asChild variant="outline" size="sm">
            <Link href="/">
              <Plus aria-hidden="true" />
              Add site
            </Link>
          </Button>
          <UserMenu />
        </div>
      </div>
    </header>
  );
}
