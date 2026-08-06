import Link from "next/link";

/**
 * Nothing to show, on purpose — a fresh database, or every website deleted.
 *
 * Quiet rather than a call to action with a big button: adding a site belongs to the landing
 * page's input (that is where creation lives, and this screen deliberately does not
 * duplicate it), so the useful thing this state can do is say so in one line and point at
 * it. An empty state that shouts is worse than one that explains.
 */
export function CrawlsEmpty() {
  return (
    <div className="flex flex-col items-center gap-2 rounded-xl border border-border bg-card px-6 py-16 text-center">
      <p className="text-sm font-medium text-foreground">No crawls yet</p>
      <p className="max-w-sm text-sm text-muted-foreground">
        Add a site from the{" "}
        <Link href="/" className="text-primary underline-offset-4 hover:underline">
          home page
        </Link>{" "}
        to generate its llms.txt.
      </p>
    </div>
  );
}
