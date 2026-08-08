"use client"

import * as React from "react"
import { Checkbox as CheckboxPrimitive } from "radix-ui"
import { CheckIcon } from "lucide-react"

import { cn } from "@/lib/utils"

// Mirrors components/ui/switch.tsx (style: radix-nova): same unified `radix-ui` import, same
// `data-checked`/`data-unchecked` custom variants (defined once in shadcn/tailwind.css, see
// that file's own `@custom-variant` block), same token vocabulary, and — per CLAUDE.md rule
// 7 — zero `dark:` variants. Added for PER-194's add-site form, which needed a checkbox where
// every other binary control in this app so far has been a `Switch`; see
// `components/landing/site-url-field.tsx` for the one call site.

function Checkbox({
  className,
  ...props
}: React.ComponentProps<typeof CheckboxPrimitive.Root>) {
  return (
    <CheckboxPrimitive.Root
      data-slot="checkbox"
      className={cn(
        "peer relative size-4 shrink-0 rounded-[4px] border border-input outline-none transition-shadow after:absolute after:-inset-x-2 after:-inset-y-2 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 data-checked:border-primary data-checked:bg-primary data-checked:text-primary-foreground",
        className
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator
        data-slot="checkbox-indicator"
        className="flex items-center justify-center text-current"
      >
        <CheckIcon className="size-3.5" />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  )
}

export { Checkbox }
