/**
 * The Anthropic mark, as an inline SVG.
 *
 * Committed rather than hotlinked on purpose: `/docs` is a public page that makes no
 * external request, and a logo fetched from a CDN would be the only one on it. It is also
 * the only icon on this page that `lucide-react` does not ship, which is why it exists as a
 * component at all — everything else in `lib/docs/sections.ts` is a Lucide import.
 *
 * The signature matches Lucide's (`className`, `aria-hidden`) so that
 * `DocsSection["icon"]` can hold either one without a union at the call site. `currentColor`
 * for the same reason: the node styles the icon by setting `text-*` on it, exactly as it
 * does for the six Lucide nodes.
 */
export function AnthropicMark({
  className,
  ...props
}: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 46 32"
      fill="currentColor"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      {...props}
    >
      <path d="M32.73 0h-6.945L38.45 32h6.945L32.73 0ZM12.665 0 0 32h7.082l2.59-6.72h13.25l2.59 6.72h7.082L19.929 0h-7.264Zm-.702 19.337 4.334-11.246 4.334 11.246h-8.668Z" />
    </svg>
  );
}
