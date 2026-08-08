/**
 * A reference table for the `/docs` MDX pages — first used by `app/docs/features/page.mdx`'s
 * Limits section, the canonical statement of the run and account caps (PER-192).
 *
 * There is no `remark-gfm` in this project (CLAUDE.md: no new dependency), so pipe-table
 * markdown (`| a | b |`) is not available, and `mdx-components.tsx` cannot style this by
 * overriding `table`/`th`/`td` the way it overrides `h2` or `blockquote`: those overrides
 * only ever fire for elements markdown *syntax* produces, and a `<table>` written as literal
 * HTML in an `.mdx` file compiles straight to a JSX intrinsic that never reaches that map —
 * see the comment in `mdx-components.tsx` where the table entries used to be, for how that
 * was confirmed rather than assumed.
 *
 * So this is an ordinary component, imported into an `.mdx` file and used like any other:
 *
 *     import { DocsTable } from "@/components/docs/docs-table";
 *
 *     <DocsTable>
 *       <thead><tr><th>Cap</th><th>Value</th></tr></thead>
 *       <tbody><tr><td>Pages fetched</td><td>100</td></tr></tbody>
 *     </DocsTable>
 *
 * `thead`/`tr`/`th`/`td` inside stay bare, unstyled HTML — this component styles them from
 * the outside with Tailwind's `[&_th]:`/`[&_td]:` descendant-selector arbitrary variants, the
 * same trick `mdx-components.tsx`'s `blockquote` (`[&_p]:text-foreground`) and `pre`
 * (`[&_code]:border-0`) already use for the same reason: plain CSS descendant selectors style
 * a child regardless of how that child's DOM node was created, so this needs no `thead`/`th`/
 * `td` components of its own.
 *
 * The wrapping `<div>` is what keeps a wide table from overflowing the page at 375px — it
 * scrolls horizontally on its own, inside its own border, rather than widening the column.
 */
export function DocsTable({ children }: { children: React.ReactNode }) {
  return (
    <div className="my-5 overflow-x-auto rounded-lg border border-border">
      <table
        className={[
          "w-full border-collapse text-left text-sm/6",
          "[&_th]:border-b [&_th]:border-border [&_th]:bg-card [&_th]:px-3 [&_th]:py-2",
          "[&_th]:text-left [&_th]:text-xs [&_th]:font-medium [&_th]:tracking-wide",
          "[&_th]:text-muted-foreground [&_th]:uppercase",
          "[&_td]:border-t [&_td]:border-border [&_td]:px-3 [&_td]:py-2 [&_td]:align-top",
          "[&_td]:text-muted-foreground",
        ].join(" ")}
      >
        {children}
      </table>
    </div>
  );
}
