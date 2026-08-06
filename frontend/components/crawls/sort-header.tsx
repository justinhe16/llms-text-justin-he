"use client";

import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";

import { TableHead } from "@/components/ui/table";
import { ariaSortFor, type SortKey, type SortState } from "@/lib/crawls/sort";
import { cn } from "@/lib/utils";

interface SortHeaderProps {
  label: string;
  sortKey: SortKey;
  state: SortState;
  onSort: (key: SortKey) => void;
  className?: string;
}

/**
 * A sortable column heading.
 *
 * `aria-sort` on the `<th>` is the part that is easy to leave out and the part that matters
 * most: the arrow glyph beside the label tells a sighted reader which column is sorted and
 * which way, and tells a screen reader nothing at all. The attribute is what makes the same
 * fact available to both.
 *
 * The inactive columns keep a faded up/down glyph rather than showing nothing until hover,
 * so it is discoverable that they *can* be sorted without having to sweep the mouse across
 * the header.
 */
export function SortHeader({ label, sortKey, state, onSort, className }: SortHeaderProps) {
  const sort = ariaSortFor(state, sortKey);
  const isActive = sort !== "none";
  const Icon = !isActive ? ChevronsUpDown : sort === "ascending" ? ArrowUp : ArrowDown;

  return (
    <TableHead aria-sort={sort} className={cn("h-10 px-3", className)}>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={cn(
          "-mx-1.5 inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-xs font-medium tracking-wide uppercase transition-colors outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50",
          isActive ? "text-foreground" : "text-muted-foreground"
        )}
      >
        {label}
        <Icon
          aria-hidden="true"
          className={cn("size-3", isActive ? "opacity-80" : "opacity-40")}
        />
      </button>
    </TableHead>
  );
}

/** A column heading that is not sortable — Pages, Schedule, Owner. Same type scale as the
 * sortable ones so the header row reads as one row rather than two kinds of thing. */
export function PlainHeader({
  label,
  className,
}: {
  label: string;
  className?: string;
}) {
  return (
    <TableHead
      className={cn(
        "h-10 px-3 text-xs font-medium tracking-wide text-muted-foreground uppercase",
        className
      )}
    >
      {label}
    </TableHead>
  );
}
