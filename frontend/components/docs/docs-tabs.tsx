"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

// The three tabs `/docs` renders, in DISPLAY order — left to right, Features first. Kept
// here rather than in `lib/docs/` — ARCHITECTURE.md §8.4's line is "anything in
// `lib/<feature>/` should be readable without knowing what the screen looks like", and three
// hrefs and three labels are markup data, not logic.
//
// Display order is NOT route order, and that is deliberate rather than a bug: `/docs` still
// serves the run tab, not Features. `/docs#fetch` is a published, external deep link this
// app cannot break, and a route group (`app/docs/(run)/`) is what keeps that tab at `/docs`
// while Features sits at its own `/docs/features`. So a tab's position in this array says
// only where it sits in the row; its `href` says where it navigates, and the two are allowed
// to disagree. If moving this entry to the front looks like it should move `/docs` with it,
// it should not — see `app/docs/layout.tsx`'s docstring for the deep-link argument in full.
const TABS = [
  { href: "/docs/features", label: "Features" },
  { href: "/docs", label: "How a run works" },
  { href: "/docs/architecture", label: "Architecture" },
] as const;

/**
 * The tab nav between `/docs`'s three routes.
 *
 * ## Links, not tabs
 *
 * `role="tablist"` / `role="tab"` / `aria-selected` describe a widget that swaps panels
 * inside one document without navigating — the ARIA Authoring Practices' own tabs pattern.
 * These are three different documents, each with its own URL, its own `metadata`, and its
 * own back/forward history entry; a screen reader announcing "tab 1 of 3" would promise a
 * kind of state restoration (arrow-key panel switching, one panel hidden from the
 * accessibility tree) that a navigation cannot deliver. `<nav>` plus `aria-current="page"` is
 * the pattern this repository already uses for "which one of several links is the current
 * page" — it is the same reasoning `components/crawls/trends-window-switcher.tsx` gives for
 * choosing `aria-pressed` over `aria-current` for *its* control: pick the ARIA vocabulary the
 * widget actually is, not the one that looks closest visually. That component is buttons
 * changing one view's data with no navigation at all, which is `aria-pressed`'s exact use
 * case; this one is links between documents, which is `aria-current="page"`'s.
 *
 * ## Exact-match active state
 *
 * `pathname === tab.href`, never `pathname.startsWith(tab.href)`. `/docs/architecture` and
 * `/docs/features` both start with `/docs`, so a `startsWith` check would light two tabs at
 * once — first the moment `/docs/architecture` existed, and again the moment `/docs/features`
 * did. This comment is the reason nobody should "simplify" that equality check later.
 */
export function DocsTabs() {
  const pathname = usePathname();

  return (
    <nav aria-label="Documentation sections" className="mt-8 flex gap-6 border-b border-border">
      {TABS.map((tab) => {
        const isActive = pathname === tab.href;

        return (
          <Link
            key={tab.href}
            href={tab.href}
            data-docs-tab={tab.href}
            aria-current={isActive ? "page" : undefined}
            className={cn(
              "-mb-px border-b-2 pb-2.5 text-sm transition-colors outline-none",
              "focus-visible:ring-3 focus-visible:ring-ring/50",
              isActive
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
