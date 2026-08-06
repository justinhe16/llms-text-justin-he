import type { LucideIcon } from "lucide-react";

/**
 * A tab that exists in the shell but whose feature has not been built yet — the Schedule
 * panel, until PER-168 lands. Trends was the other one; PER-169 has replaced it, so this
 * component now has exactly one call site left.
 *
 * ## Why these are rendered at all
 *
 * Hiding a not-yet-built tab and adding it later moves every other tab sideways at the
 * moment a user has just learned where things are, and it hides the page's intended shape
 * from everyone reviewing it in between. Showing it, visibly inert, is honest about both.
 *
 * ## Replacing one
 *
 * Swap the `<PlaceholderTab .../>` for the real panel in
 * `components/crawls/website-detail.tsx` and delete nothing else. This component takes no
 * dependency on either feature, and neither feature needs to touch it — once PER-168 replaces
 * the last one, this file has no call sites left and should be deleted along with it.
 */
export function PlaceholderTab({
  icon: Icon,
  title,
  description,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
}) {
  return (
    // `select-none` and the muted palette are the whole visual argument: this panel should
    // read as scaffolding at a glance, without a "coming soon" ribbon shouting it.
    <div className="flex select-none flex-col items-center justify-center rounded-lg border border-dashed border-border bg-card/50 px-6 py-16 text-center">
      <Icon className="size-5 text-muted-foreground/60" aria-hidden />
      <p className="mt-3 text-sm font-medium text-muted-foreground">{title}</p>
      <p className="mt-1 max-w-sm text-sm text-muted-foreground/80">{description}</p>
      <p className="mt-4 text-xs tracking-wide text-muted-foreground/70 uppercase">
        Coming soon
      </p>
    </div>
  );
}
