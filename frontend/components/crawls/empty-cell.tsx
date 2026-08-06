/**
 * The em dash a cell shows when it has no value.
 *
 * A bare "—" in the markup is read aloud by a screen reader as "em dash", or skipped
 * entirely, and either way says nothing about *why* the cell is empty. The visible glyph is
 * therefore `aria-hidden` and paired with a short screen-reader-only phrase that says what
 * the dash means in that column — "no pages counted", "no schedule".
 */
export function EmptyCell({ label }: { label: string }) {
  return (
    <>
      <span aria-hidden="true" className="text-muted-foreground">
        —
      </span>
      <span className="sr-only">{label}</span>
    </>
  );
}
