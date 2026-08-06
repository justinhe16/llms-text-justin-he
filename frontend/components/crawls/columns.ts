// Column widths and row height for the /crawls table, in one place because two components
// have to agree on them exactly.
//
// The skeleton (crawls-skeleton.tsx) and the real table (crawls-table.tsx) render different
// content into the same grid, and the entire point of a skeleton over a spinner is that
// nothing moves when the data arrives. If the two files each carried their own widths, they
// would agree on the day they were written and drift the first time a column was adjusted —
// producing exactly the content jump the skeleton exists to prevent, in a way nobody would
// notice until they looked closely at a slow load.
//
// Percentages, with `table-fixed` on the `<table>`: fixed layout is also what makes
// `truncate` work inside a cell at all. Under the default `auto` layout a table grows to fit
// its widest cell instead of clipping it, so a long page title would push the table wider
// than its container rather than ellipsing.

export const COLUMN_WIDTH = {
  site: "w-[30%]",
  status: "w-[15%]",
  pages: "w-[9%]",
  lastRun: "w-[15%]",
  schedule: "w-[13%]",
  owner: "w-[18%]",
} as const;

/** Every row, real or skeleton, is this tall. */
export const ROW_HEIGHT = "h-[3.25rem]";

/** How many skeleton rows to draw. Enough to read as a table rather than as a single
 * placeholder bar, few enough not to imply a page of results that may not exist. */
export const SKELETON_ROW_COUNT = 5;
