import React, { type ComponentPropsWithoutRef, type CSSProperties } from "react"
import { Slot } from "radix-ui"

import { cn } from "@/lib/utils"

export interface ShimmerButtonProps extends ComponentPropsWithoutRef<"button"> {
  shimmerColor?: string
  shimmerSize?: string
  borderRadius?: string
  shimmerDuration?: string
  background?: string
  className?: string
  children?: React.ReactNode
  /**
   * Render the child element instead of a `<button>`, keeping every style and the
   * shimmer layers. Added — the same `Slot`-based escape hatch `components/ui/button.tsx`
   * already exposes — because the landing page's primary action when you are signed in is
   * a *navigation* to /crawls, and a `<button>` that calls `router.push` breaks
   * middle-click, cmd-click and "copy link address" on the page's main call to action.
   * Wrapping the button in an `<a>` instead is invalid HTML (interactive content inside a
   * link), so the element itself has to become the link.
   */
  asChild?: boolean
}

export const ShimmerButton = React.forwardRef<
  HTMLButtonElement,
  ShimmerButtonProps
>(
  (
    {
      // Retuned for the light theme. The upstream defaults are a black button
      // with a white shimmer; here the button is the one accent colour from
      // app/globals.css, so a palette change stays a one-file edit.
      shimmerColor = "#ffffff",
      shimmerSize = "0.05em",
      shimmerDuration = "3s",
      borderRadius = "100px",
      background = "var(--primary)",
      className,
      children,
      asChild = false,
      ...props
    },
    ref
  ) => {
    const Comp = asChild ? Slot.Root : "button"

    return (
      <Comp
        style={
          {
            "--spread": "90deg",
            "--shimmer-color": shimmerColor,
            // Upstream names this `--radius`, which would shadow the global
            // radius token from app/globals.css for everything nested inside
            // the button. Scoped to the component instead.
            "--shimmer-radius": borderRadius,
            "--speed": shimmerDuration,
            "--cut": shimmerSize,
            "--bg": background,
          } as CSSProperties
        }
        className={cn(
          "group relative z-0 flex cursor-pointer items-center justify-center overflow-hidden [border-radius:var(--shimmer-radius)] border border-white/10 px-6 py-3 whitespace-nowrap text-primary-foreground [background:var(--bg)]",
          "transform-gpu transition-transform duration-300 ease-in-out active:translate-y-px",
          className
        )}
        ref={ref}
        {...props}
      >
        {/* spark container */}
        <div
          className={cn(
            "-z-30 blur-[2px]",
            "@container-[size] absolute inset-0 overflow-visible"
          )}
        >
          {/* spark */}
          <div className="animate-shimmer-slide absolute inset-0 aspect-[1] h-[100cqh] rounded-none [mask:none]">
            {/* spark before */}
            <div className="animate-spin-around absolute -inset-full w-auto [translate:0_0] rotate-0 [background:conic-gradient(from_calc(270deg-(var(--spread)*0.5)),transparent_0,var(--shimmer-color)_var(--spread),transparent_var(--spread))]" />
          </div>
        </div>
        {/* `Slottable` is not decoration, and removing it breaks `asChild` at runtime only.
            `Slot` requires a single React element child so it knows what to clone; this
            component hands it four (three shimmer layers plus `children`), and without this
            marker it throws "Slot failed to slot onto its children" the moment an `asChild`
            instance renders — a client-side crash that `tsc`, eslint, `next build` and the
            smoke test all pass straight through. Marking `children` tells `Slot` which one
            becomes the rendered element; the three layers stay, as its children. */}
        <Slot.Slottable>{children}</Slot.Slottable>

        {/* Highlight */}
        <div
          className={cn(
            "absolute inset-0 size-full",

            "rounded-2xl px-4 py-1.5 text-sm font-medium shadow-[inset_0_-8px_10px_#ffffff1f]",

            // transition
            "transform-gpu transition-all duration-300 ease-in-out",

            // on hover
            "group-hover:shadow-[inset_0_-6px_10px_#ffffff3f]",

            // on click
            "group-active:shadow-[inset_0_-10px_10px_#ffffff3f]"
          )}
        />

        {/* backdrop */}
        <div
          className={cn(
            "absolute inset-(--cut) -z-20 [border-radius:var(--shimmer-radius)] [background:var(--bg)]"
          )}
        />
      </Comp>
    )
  }
)

ShimmerButton.displayName = "ShimmerButton"
