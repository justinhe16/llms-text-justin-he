"use client";

import { RefreshCw, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface CrawlsErrorProps {
  /** `ApiError.message` carries the backend's own wording — see lib/api/fetcher.ts's
   * `extractErrorMessage`. A network failure is a native `TypeError`, whose message
   * ("Failed to fetch") is unhelpful, so that case falls back to a sentence of our own. */
  error: Error;
  onRetry: () => void;
  isRetrying: boolean;
  /** `true` when there is already data on screen. */
  hasStaleData: boolean;
}

const NETWORK_FAILURE_MESSAGE = "Could not reach the server.";

function describe(error: Error): string {
  // `apiFetch` only builds an `ApiError` from a *response*; anything that stops the request
  // reaching a server at all propagates as whatever `fetch` threw, which is a `TypeError` in
  // every browser and reads "Failed to fetch" / "NetworkError when attempting to fetch
  // resource". Neither is worth showing to anyone.
  if (error instanceof TypeError) return NETWORK_FAILURE_MESSAGE;
  return error.message || NETWORK_FAILURE_MESSAGE;
}

/**
 * The error state, in two shapes for two genuinely different situations.
 *
 * With no data yet, this is the whole content area: there is nothing else to look at, and
 * the message plus a retry button is the entire screen's job.
 *
 * With data already on screen — a poll that failed after several that succeeded — it is a
 * strip above the table, and **the table stays exactly where it was**. Blanking a rendered
 * table because one background refetch failed throws away everything the reader was looking
 * at to tell them something they did not ask about; the honest version says "this is a few
 * seconds old and here is why" and leaves the data alone. React Query keeps the last
 * successful `data` through a failed refetch, so this costs nothing but the decision to use
 * it.
 */
export function CrawlsError({ error, onRetry, isRetrying, hasStaleData }: CrawlsErrorProps) {
  const message = describe(error);

  return (
    <div
      role="alert"
      className={cn(
        "flex items-center gap-3 rounded-lg border border-border bg-status-failed-surface px-4",
        hasStaleData ? "py-2.5" : "flex-col justify-center py-16 text-center"
      )}
    >
      <TriangleAlert
        aria-hidden="true"
        className={cn("shrink-0 text-status-failed", hasStaleData ? "size-4" : "size-5")}
      />
      <div className={cn("min-w-0", hasStaleData ? "flex-1" : "")}>
        <p className="text-sm font-medium text-foreground">
          {hasStaleData ? "Couldn’t refresh" : "Couldn’t load crawls"}
        </p>
        <p className="truncate text-sm text-muted-foreground">{message}</p>
      </div>
      <Button variant="outline" size="sm" onClick={onRetry} disabled={isRetrying}>
        <RefreshCw aria-hidden="true" className={cn(isRetrying && "animate-spin")} />
        {isRetrying ? "Retrying…" : "Retry"}
      </Button>
    </div>
  );
}
