"use client";

import { useMemo, useRef } from "react";
import { CopyIcon, DownloadIcon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { llmsTxtFilename } from "@/lib/crawls/run-display";

/**
 * The `llms.txt` artifact itself: line numbers, a copy button, a download button, and
 * horizontal scrolling that stays inside the viewer.
 *
 * ## Why this is two `<pre>` elements and not 5,000 `<div>`s
 *
 * The ticket's constraint is that a large artifact must not jank the page, and offers
 * "virtualize or cap the rendered height" as the two ways out. This does something simpler
 * and strictly better than either: it renders the whole document as exactly **two text
 * nodes** — one for the gutter, one for the code — inside a fixed-height scroll container.
 *
 * The jank a 5,000-line artifact causes is not the browser struggling with text; browsers
 * lay out large `<pre>` blocks very well. It is React reconciling 5,000 elements and the
 * style/layout cost of the DOM nodes behind them. Emitting the lines as one string removes
 * that cost entirely rather than deferring it, and the node count is O(1) in the size of
 * the file — a 50,000-line artifact renders exactly as fast as a 5-line one.
 *
 * It also avoids what virtualization would have cost here: a windowing library (a
 * dependency this repo does not have), a measured row height that breaks the moment a line
 * wraps, and a `Ctrl+F` that only searches the visible window. Native text search works
 * over this viewer because the whole document is really in the DOM.
 *
 * The gutter and the code are aligned by nothing more than a shared `leading-5` — two
 * `<pre>`s in the same font and the same line-height have the same baselines. The gutter is
 * `sticky left-0` so it stays put while the code scrolls horizontally under it.
 */
export function LlmsTxtViewer({ content, origin }: { content: string; origin: string }) {
  const codeRef = useRef<HTMLPreElement>(null);

  // Split once per artifact, not once per render: a poll tick re-renders this component
  // every three seconds while a *different* run is still going, and re-splitting a
  // 5,000-line string each time would be work with no output change.
  const { lineNumbers, lineCount } = useMemo(() => {
    // A trailing newline would otherwise produce a final, empty numbered line — every text
    // file ends with one, so this is the common case rather than an edge case.
    const normalized = content.endsWith("\n") ? content.slice(0, -1) : content;
    const count = normalized.length === 0 ? 1 : normalized.split("\n").length;
    return {
      lineNumbers: Array.from({ length: count }, (_, index) => index + 1).join("\n"),
      lineCount: count,
    };
  }, [content]);

  const copy = async () => {
    // `navigator.clipboard` is undefined outside a secure context, and even inside one the
    // write can be refused by permissions policy — so both the absence and the rejection
    // have to be handled, and neither may end in nothing visibly happening.
    try {
      if (!navigator.clipboard) throw new Error("The clipboard API is unavailable.");
      await navigator.clipboard.writeText(content);
      toast.success("llms.txt copied to your clipboard");
    } catch {
      // The fallback selects the artifact so the user's own Cmd/Ctrl+C works, which needs
      // no permission at all. Telling them to press it is the point — silently doing
      // nothing, or a bare "copy failed", leaves them with no way to get the text.
      const selected = selectElementText(codeRef.current);
      toast.error(
        selected
          ? "Couldn't copy automatically — the text is selected, press Cmd/Ctrl+C."
          : "Couldn't copy. Use Download instead."
      );
    }
  };

  const download = () => {
    // A Blob and an object URL, not a `data:` URI: a multi-megabyte artifact exceeds what
    // some browsers accept in a URL, and the object URL costs nothing to create.
    const url = URL.createObjectURL(new Blob([content], { type: "text/plain;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = llmsTxtFilename(origin);
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    // Revoking immediately is safe — the download has already been handed to the browser
    // by `click()` — and not revoking leaks the whole artifact for the life of the document.
    URL.revokeObjectURL(url);
    toast.success(`Downloaded ${llmsTxtFilename(origin)}`);
  };

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card">
      <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-2">
        <p className="font-mono text-xs text-muted-foreground">
          {lineCount.toLocaleString()} {lineCount === 1 ? "line" : "lines"}
        </p>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={copy}>
            <CopyIcon aria-hidden />
            Copy
          </Button>
          <Button variant="outline" size="sm" onClick={download}>
            <DownloadIcon aria-hidden />
            Download
          </Button>
        </div>
      </div>

      {/*
        The ONLY scroll container in this component, and the reason the page body never
        scrolls sideways no matter how long a line in the artifact is: `overflow-auto`
        here, and `min-w-0` on every flex ancestor up to the page (see output-tab.tsx and
        website-detail.tsx), keep the overflow from propagating outward.

        `max-h-[70vh]` caps the height so the viewer scrolls internally instead of making
        the page arbitrarily long — the ticket's "cap the rendered height" — while still
        being tall enough on a laptop to read an artifact in.
      */}
      <div className="max-h-[70vh] overflow-auto">
        <div className="flex min-w-max">
          <pre
            aria-hidden
            className="sticky left-0 z-10 shrink-0 border-r border-border bg-muted/40 px-3 py-3 text-right font-mono text-xs leading-5 tabular-nums text-muted-foreground select-none"
          >
            {lineNumbers}
          </pre>
          {/* `whitespace-pre`, not `pre-wrap`: long lines must scroll, not wrap. Wrapping
              an llms.txt would misrepresent the file — its line structure is meaningful. */}
          <pre
            ref={codeRef}
            className="px-4 py-3 font-mono text-xs leading-5 whitespace-pre text-foreground"
          >
            {content}
          </pre>
        </div>
      </div>
    </div>
  );
}

/**
 * Selects an element's text in the document, returning whether it worked — the clipboard
 * fallback above. Guards every step because this only ever runs on a path where something
 * has already gone wrong, and a `TypeError` thrown out of an error handler would replace a
 * recoverable problem with an unrecoverable one.
 */
function selectElementText(element: HTMLElement | null): boolean {
  if (element === null) return false;
  const selection = window.getSelection();
  if (selection === null) return false;

  const range = document.createRange();
  range.selectNodeContents(element);
  selection.removeAllRanges();
  selection.addRange(range);
  return true;
}
