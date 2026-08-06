import Link from "next/link";

import type { WebsiteListItem } from "@/lib/api/websites";
import { crawlDetailPath } from "@/lib/crawls/row-activation";
import { cn } from "@/lib/utils";

interface CrawlSiteProps {
  website: WebsiteListItem;
  className?: string;
}

/**
 * The Site cell: the origin in Geist Mono, with the page title underneath as quiet secondary
 * text when the backend has one.
 *
 * The origin is a real `<a>` even though the row it sits in is already clickable
 * (lib/crawls/row-activation.ts). Three things come free with that and with nothing else:
 * assistive technology listing this table's rows as links, ⌘-click opening a site in a new
 * tab, and the browser's own status bar showing where the row goes on hover.
 *
 * `tabIndex={-1}` keeps it out of the tab order. The row is the tab stop; a link inside it
 * that is also a tab stop would mean two presses per row to get down the table, both of
 * which do exactly the same thing.
 */
export function CrawlSite({ website, className }: CrawlSiteProps) {
  return (
    <span className={cn("flex min-w-0 flex-col gap-0.5", className)}>
      <Link
        href={crawlDetailPath(website.id)}
        tabIndex={-1}
        className="truncate font-mono text-sm text-foreground hover:underline"
      >
        {website.origin}
      </Link>
      {website.title ? (
        <span className="truncate text-xs text-muted-foreground">{website.title}</span>
      ) : null}
    </span>
  );
}
