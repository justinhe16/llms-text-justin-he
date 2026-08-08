"use client";

import { Skeleton } from "@/components/ui/skeleton";
import type { StatsIndexDiff, StatsIndexPageRef, StatsLatest } from "@/lib/api/runs";
import { METADATA_NOT_COMPARABLE } from "@/lib/crawls/enrichment-copy";
import { signedCount } from "@/lib/crawls/stats-display";

import { EmptyCell } from "./empty-cell";
import { RelativeTime } from "./relative-time";

/**
 * "What changed in the latest run" — the added/removed/content-changed/metadata-changed page
 * lists for the newest completed run in the selected window, and the secondary metrics beside
 * them (PER-193, retrofitted for enrichment modes by PER-194).
 *
 * *Mirrors* `ChartCard`'s card shell (`trends-charts.tsx`) for the section/heading/description
 * layout, and `OutputTab`'s "list with an explicit empty state" idiom (`output-tab.tsx`) —
 * this is a list, not a chart, but it lives in the same panel and should look like it belongs
 * beside the three that are.
 *
 * ## Five branches, driven by `diff_state` plus `previous_run_completed`, never by a count
 *
 * | `latest` | `diff_state` | `previous_run_completed` | message |
 * | --- | --- | --- | --- |
 * | `null` | — | — | "No completed run in this window." |
 * | present | `"not_recorded"` | any | "No comparison available for this run." |
 * | present | `"first_run"` | `null` | "First run of this site — nothing to compare against yet." |
 * | present | `"first_run"` | `false` | "No completed run before this one to compare against — every earlier run failed." |
 * | present | `"compared"` | `true` | the numbers, no extra message |
 * | present | `"compared"` | `false` | the numbers, plus a note that the run before this one didn't complete |
 *
 * Never a zero check: a run that legitimately added, removed, and changed nothing still shows
 * four "No pages …" subsections rather than falling through to one of the messages above,
 * because that IS a real comparison, and collapsing "compared, nothing changed" into "nothing
 * to compare" would misreport an outcome as an absence of data.
 *
 * ## The fourth column, added by PER-194
 *
 * Inside the `"compared"` branch, "Titles & descriptions" (`diff.metadata_changed`) can
 * itself be not-comparable — `null`, with `diff.metadata_not_comparable_reason` naming which
 * direction an enrichment toggle flipped since the previous run — while "Content changed"
 * (`diff.content_changed`) is independent of that and reports normally unless the previous
 * run simply recorded no page fingerprints (`null` with no reason, the same "the previous row
 * didn't record it" rule `urls_discovered_delta` in the footer already uses). Neither
 * `null` is a zero: a `null` "Titles & descriptions" cell renders `METADATA_NOT_COMPARABLE`'s
 * sentence for the direction that flipped, and a `null` "Content changed" cell renders the
 * same `EmptyCell` treatment `urls_discovered_delta` gets below.
 */
export function TrendsWhatChanged({ latest }: { latest: StatsLatest | null }) {
  return (
    <section className="min-w-0 rounded-xl border border-border bg-card p-4">
      <h3 className="text-sm font-medium text-foreground">What changed in the latest run</h3>
      <p className="mt-0.5 text-xs text-muted-foreground">
        Pages added, removed, or changed in the index, against the previous completed run.
      </p>

      <div className="mt-3">
        <TrendsWhatChangedBody latest={latest} />
      </div>
    </section>
  );
}

/**
 * The loading state — same card shell, same three-column grid, so the panel does not resize
 * when `stats.latest` lands. Not named in the plan's own inventory, but added for the same
 * reason every other skeleton in this panel is sized to match its loaded counterpart
 * (`TrendsStatTilesSkeleton`, `TrendsChartsSkeleton`): a loading state substantially shorter
 * than the content it precedes produces exactly the layout jump skeletons exist to prevent.
 */
export function TrendsWhatChangedSkeleton() {
  return (
    <div aria-hidden="true" className="rounded-xl border border-border bg-card p-4">
      <Skeleton className="h-4 w-56" />
      <Skeleton className="mt-2 h-3 w-72" />
      <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }, (_, index) => (
          <div key={index} className="space-y-2">
            <Skeleton className="h-3 w-16" />
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-3/4" />
          </div>
        ))}
      </div>
    </div>
  );
}

function TrendsWhatChangedBody({ latest }: { latest: StatsLatest | null }) {
  if (latest === null) {
    return <EmptyMessage>No completed run in this window.</EmptyMessage>;
  }

  switch (latest.diff_state) {
    case "not_recorded":
      return <EmptyMessage>No comparison available for this run.</EmptyMessage>;

    case "first_run":
      return (
        <EmptyMessage>
          {latest.previous_run_completed === false
            ? "No completed run before this one to compare against — every earlier run failed."
            : "First run of this site — nothing to compare against yet."}
        </EmptyMessage>
      );

    case "compared":
      // `diff` is non-null exactly when `diff_state === "compared"`
      // (`LatestRunSnapshot.diff`'s own docstring) — TypeScript cannot narrow across two
      // independent fields, so this is the one place in this component that assumes it,
      // guarded by a fallback that renders nothing rather than throwing if the backend's own
      // invariant were ever violated.
      return latest.diff === null ? null : (
        <ComparedBody previousRunCompleted={latest.previous_run_completed} diff={latest.diff} />
      );
  }
}

function EmptyMessage({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-muted-foreground">{children}</p>;
}

function ComparedBody({
  previousRunCompleted,
  diff,
}: {
  previousRunCompleted: boolean | null;
  diff: StatsIndexDiff;
}) {
  return (
    <div className="space-y-4">
      {previousRunCompleted === false && (
        <p className="text-xs text-muted-foreground">
          The run before this one didn&apos;t complete. Compared with the last completed run
          instead.
        </p>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <ChangeSection
          title="Added"
          count={diff.pages_added}
          sample={diff.added_sample}
          emptyLabel="No pages added"
        />
        <ChangeSection
          title="Removed"
          count={diff.pages_removed}
          sample={diff.removed_sample}
          emptyLabel="No pages removed"
        />
        <ChangeSection
          title="Content changed"
          // `?? null`/`?? []`: `content_changed`/`content_changed_sample` carry Python-side
          // defaults (for version-7 row compatibility — `RunIndexDiff`'s own docstring), which
          // makes openapi-typescript mark them optional even though a live response always
          // includes them; this is the same defensive read `latest_run`'s own `?? null`
          // callers already use for a field in the identical shape.
          count={diff.content_changed ?? null}
          sample={diff.content_changed_sample ?? []}
          emptyLabel="No pages changed"
          nullContent={<EmptyCell label="the previous run recorded no page fingerprints" />}
        />
        <ChangeSection
          title="Titles & descriptions"
          count={diff.metadata_changed}
          sample={diff.metadata_changed_sample}
          emptyLabel="No pages changed"
          nullContent={
            !diff.metadata_not_comparable_reason
              ? null
              : METADATA_NOT_COMPARABLE[diff.metadata_not_comparable_reason]
          }
        />
      </div>

      <TrendsWhatChangedFooter diff={diff} />
    </div>
  );
}

/**
 * One of the four Added / Removed / Content changed / Titles & descriptions lists.
 *
 * `count === null` (PER-194: `content_changed`/`metadata_changed` only — `pages_added`/
 * `pages_removed` are never null) renders `nullContent` in place of both the number in the
 * heading and the list, which is why it is checked before the `count === 0` empty state: a
 * `null` count is "not comparable", a `0` count is "compared, and nothing changed" — two
 * different facts that must never render the same way.
 */
function ChangeSection({
  title,
  count,
  sample,
  emptyLabel,
  nullContent,
}: {
  title: string;
  count: number | null;
  sample: StatsIndexPageRef[];
  emptyLabel: string;
  /** What to render in place of the count and the list when `count` is `null`. Omitted for
   * the two fields that are never null. */
  nullContent?: React.ReactNode;
}) {
  if (count === null) {
    return (
      <div className="min-w-0">
        <p className="text-xs font-medium text-foreground">{title}</p>
        <p className="mt-1 text-xs text-muted-foreground">{nullContent}</p>
      </div>
    );
  }

  return (
    <div className="min-w-0">
      <p className="text-xs font-medium text-foreground">
        {title} (<span className="tabular-nums">{count.toLocaleString()}</span>)
      </p>

      {count === 0 ? (
        <p className="mt-1 text-xs text-muted-foreground">{emptyLabel}</p>
      ) : (
        <ul className="mt-1 space-y-1">
          {sample.map((page) => (
            <li key={page.url} className="min-w-0">
              {/* The app's first `target="_blank"` links. Every URL here is a page on the
                  crawled SITE, not on this dashboard — off-origin by definition — so opening
                  it in the current tab would navigate the reader away from the app entirely.
                  `rel="noopener noreferrer"` is mandatory alongside it: without `noopener` the
                  new tab gets a live `window.opener` back to this one, and a site whose
                  content this app does not control could use it to redirect the tab the
                  reader came from. */}
              <a
                href={page.url}
                target="_blank"
                rel="noopener noreferrer"
                className="block truncate text-xs text-foreground underline decoration-border underline-offset-2 hover:text-primary"
              >
                {page.title ?? page.url}
              </a>
            </li>
          ))}
          {/* Always with the number — never a bare "and more". */}
          {sample.length < count && (
            <li className="text-xs text-muted-foreground">and {count - sample.length} more</li>
          )}
        </ul>
      )}
    </div>
  );
}

/** The "compared with" line and the secondary metrics strip beneath the three lists. */
function TrendsWhatChangedFooter({ diff }: { diff: StatsIndexDiff }) {
  return (
    <div className="space-y-2 border-t border-border pt-3">
      {diff.compared_to_completed_at !== null && (
        <p className="text-xs text-muted-foreground">
          Compared with the run of <RelativeTime iso={diff.compared_to_completed_at} />
        </p>
      )}

      <dl className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <div className="flex items-center gap-1">
          <dt>URLs discovered</dt>
          <dd className="text-foreground">
            {diff.urls_discovered_delta === null ? (
              <EmptyCell label="not recorded on the previous run" />
            ) : (
              <span className="tabular-nums">{signedCount(diff.urls_discovered_delta)}</span>
            )}
          </dd>
        </div>

        <div className="flex items-center gap-1">
          <dt>Index size</dt>
          <dd className="tabular-nums text-foreground">
            {signedCount(diff.llms_txt_bytes_delta)} bytes
          </dd>
        </div>

        <div className="flex items-center gap-1">
          <dt>Churn ratio</dt>
          <dd className="text-foreground">
            {diff.selection_churn_ratio === null ? (
              <EmptyCell label="both indexes were empty" />
            ) : (
              <span className="tabular-nums">
                {(diff.selection_churn_ratio * 100).toFixed(1)}%
              </span>
            )}
          </dd>
        </div>

        {/* Only sections whose count actually changed — see the backend's `sections_delta`
            docstring. Omitted entirely, not rendered as an empty strip, when nothing moved. */}
        {Object.keys(diff.sections_delta).length > 0 && (
          <div className="flex items-center gap-1">
            <dt>Sections</dt>
            <dd className="tabular-nums text-foreground">
              {Object.entries(diff.sections_delta)
                .map(([section, delta]) => `${section} ${signedCount(delta)}`)
                .join(", ")}
            </dd>
          </div>
        )}
      </dl>
    </div>
  );
}
