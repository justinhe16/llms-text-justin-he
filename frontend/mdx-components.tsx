import type { MDXComponents } from "mdx/types";
import Link from "next/link";

/**
 * How MDX renders into this design system.
 *
 * The file has to live at the project root with exactly this name and this export — that is
 * `@next/mdx`'s contract for the App Router, not a choice made here — which is also why it
 * is the one component file outside `components/`.
 *
 * Every element is styled explicitly rather than through a typography plugin. There is one
 * MDX page in this application, `@tailwindcss/typography` is not installed, and a plugin
 * would bring its own opinions about colour — including a `prose-invert` dark variant this
 * repository does not have and must not grow (CLAUDE.md rule 7). A dozen-odd element styles
 * that name the palette's own tokens are smaller than that and cannot drift from it.
 */
export function useMDXComponents(components: MDXComponents): MDXComponents {
  return {
    // Headings forward `id`. They do not invent it: `rehype-slug` (wired in
    // `next.config.ts`) has already put a `github-slugger` slug on every heading node by the
    // time this map runs, and dropping the prop here would throw it away and leave
    // `lib/docs/sections.ts` pointing at nothing.
    //
    // `scroll-mt-24` is 96px, and it is not a taste decision: it must stay *below*
    // `ACTIVE_LINE_PX` (112px) in `lib/docs/use-active-section.ts`. Clicking a diagram node
    // lands its heading at 96px, and 96 < 112 means that heading is unambiguously the active
    // one the moment the scroll settles. Setting the two equal puts the clicked heading
    // exactly on the boundary, where a sub-pixel shortfall in a smooth scroll leaves the node
    // you just pressed unlit. Change one of these numbers and change the other — keeping the
    // inequality in this direction.
    h1: ({ id, children }) => (
      <h1 id={id} className="mb-3 scroll-mt-24 text-3xl font-medium tracking-tight text-foreground">
        {children}
      </h1>
    ),
    h2: ({ id, children }) => (
      <h2
        id={id}
        className="mt-12 mb-3 scroll-mt-24 text-lg font-medium tracking-tight text-foreground"
      >
        {children}
      </h2>
    ),
    h3: ({ id, children }) => (
      <h3 id={id} className="mt-8 mb-2 scroll-mt-24 text-base font-medium text-foreground">
        {children}
      </h3>
    ),
    p: ({ children }) => <p className="my-4 text-sm/7 text-muted-foreground">{children}</p>,
    ul: ({ children }) => (
      <ul className="my-4 list-disc space-y-2 pl-5 text-sm/7 text-muted-foreground">
        {children}
      </ul>
    ),
    ol: ({ children }) => (
      <ol className="my-4 list-decimal space-y-2 pl-5 text-sm/7 text-muted-foreground">
        {children}
      </ol>
    ),
    li: ({ children }) => <li className="pl-1">{children}</li>,
    strong: ({ children }) => (
      <strong className="font-medium text-foreground">{children}</strong>
    ),
    hr: () => <hr className="my-10 border-border" />,
    // The callout. Lifts one paragraph — currently "what a run actually produces" — out of
    // the body copy, so the thing a reader most needs to know before trying the product is
    // visibly separate from the prose explaining it, instead of every other sentence growing
    // a qualifier.
    blockquote: ({ children }) => (
      <blockquote className="my-6 rounded-lg border border-border bg-card px-4 py-1 [&_p]:text-foreground">
        {children}
      </blockquote>
    ),
    // Inline code. A `<code>` inside a `<pre>` is handled by the `pre` rule below, which
    // sets its own colours on the block; this styling is what an identifier in a sentence
    // gets.
    code: ({ children }) => (
      <code className="rounded-md border border-border bg-card px-1.5 py-0.5 font-mono text-[0.8125rem] text-foreground">
        {children}
      </code>
    ),
    pre: ({ children }) => (
      <pre className="my-5 overflow-x-auto rounded-lg border border-border bg-card p-4 font-mono text-[0.8125rem]/6 text-foreground [&_code]:border-0 [&_code]:bg-transparent [&_code]:p-0">
        {children}
      </pre>
    ),
    // Internal links go through `next/link` so `/crawls` is a client navigation; external
    // ones stay plain anchors and open in a new tab, since leaving the docs to read the
    // llms.txt specification should not lose your place.
    a: ({ href, children }) => {
      const target = href ?? "#";
      if (target.startsWith("/")) {
        return (
          <Link
            href={target}
            className="rounded-sm font-medium text-primary underline underline-offset-4 outline-none hover:text-primary/80 focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            {children}
          </Link>
        );
      }
      return (
        <a
          href={target}
          target="_blank"
          rel="noreferrer noopener"
          className="rounded-sm font-medium text-primary underline underline-offset-4 outline-none hover:text-primary/80 focus-visible:ring-3 focus-visible:ring-ring/50"
        >
          {children}
        </a>
      );
    },
    ...components,
  };
}
