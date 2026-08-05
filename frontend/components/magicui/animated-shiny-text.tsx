import {
  type ComponentPropsWithoutRef,
  type CSSProperties,
  type FC,
} from "react"

import { cn } from "@/lib/utils"

export interface AnimatedShinyTextProps extends ComponentPropsWithoutRef<"span"> {
  shimmerWidth?: number
}

export const AnimatedShinyText: FC<AnimatedShinyTextProps> = ({
  children,
  className,
  shimmerWidth = 100,
  ...props
}) => {
  return (
    <span
      style={
        {
          "--shiny-width": `${shimmerWidth}px`,
        } as CSSProperties
      }
      className={cn(
        // Retuned for the light theme: the resting text is the muted stone,
        // and the travelling highlight is a darkening rather than a
        // lightening, which is what reads as "shine" on a warm paper base.
        // The alpha matters: the shine is a gradient clipped to the glyphs
        // (`bg-clip-text`), so the text colour has to stay translucent for it
        // to show through.
        "mx-auto max-w-md text-muted-foreground/70",

        // Shine effect
        "animate-shiny-text bg-size-[var(--shiny-width)_100%] bg-clip-text bg-position-[0_0] bg-no-repeat [transition:background-position_1s_cubic-bezier(.6,.6,0,1)_infinite]",

        // Shine gradient
        "bg-linear-to-r from-transparent via-foreground/80 via-50% to-transparent",

        className
      )}
      {...props}
    >
      {children}
    </span>
  )
}
