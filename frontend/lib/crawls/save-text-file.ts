// The one place this app turns text it already has in memory into a downloaded file. Pulled
// out of `LlmsTxtViewer.download()` (components/crawls/llms-txt-viewer.tsx) so it can be
// shared with `useDownloadFullArtifact` (lib/crawls/use-download-full-artifact.ts): the
// Output tab has two download paths now — the existing Download button, which already holds
// `llms.txt` in memory from `GET /runs/{id}`, and PER-182's "Download full" button, which
// fetches `llms-full.txt` on click and then has exactly the same "turn this string into a
// file" problem. Both funnel through the one object-URL lifecycle below instead of carrying
// two copies of it that could drift.
//
// This is a pure extraction, not a new capability: `saveTextFile` does no fetching and knows
// nothing about runs, artifacts, or the API — it takes a string a caller already has and a
// filename, and does the DOM part. Deciding *what* string and *what* filename stays with the
// caller, which is why this takes both as plain arguments rather than reaching into
// `lib/api/runs.ts` or `lib/crawls/run-display.ts` itself.

/**
 * Saves `text` as a local file named `filename`. Synchronous from the caller's point of view
 * — the browser's download UI is what happens next, not anything this function waits on.
 */
export function saveTextFile(text: string, filename: string): void {
  // A Blob and an object URL, not a `data:` URI: a multi-megabyte artifact exceeds what
  // some browsers accept in a URL, and the object URL costs nothing to create.
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Revoked on the next macrotask rather than synchronously. Every current browser has
  // handed the download off by the time `click()` returns, but some have historically
  // processed the anchor's navigation asynchronously, and a URL revoked before that lands
  // produces a silently empty file. Deferring costs one tick and removes the class of bug;
  // not revoking at all would leak the whole artifact for the document's lifetime.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
