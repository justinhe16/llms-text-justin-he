"use client"

import { useCallback, useEffect, useId, useRef, useState, type RefObject } from "react"
import { motion, useReducedMotion } from "motion/react"

import { cn } from "@/lib/utils"

/**
 * Magic UI's `animated-beam`, retuned for this repository (ARCHITECTURE.md §8.4 permits
 * retuning a Magic UI primitive in place). Four changes from the registry copy:
 *
 * 1. **Light-palette defaults.** The registry ships an orange-to-purple gradient on a grey
 *    path. The defaults here read the theme's own tokens, so a beam matches the page it is
 *    drawn on without every call site restating four colours.
 * 2. **It re-measures when a *node* moves, not only when the container resizes.** The
 *    registry observes the container alone, which is enough for a fixed grid and wrong for
 *    a column of text: a label wrapping onto a second line moves every node below it while
 *    the container's own box is unchanged, and the beams stay where they were. Every
 *    element this component measures is observed.
 * 3. **It re-measures after webfonts settle.** `document.fonts.ready` resolves after the
 *    face swap, and the swap changes the height of every label on the page. A beam measured
 *    before it lands a few pixels off — invisible on a hot reload, where the font is already
 *    cached, and obvious on a hard load. This is the bug the ticket for this component
 *    called out by name.
 * 4. **It honours `prefers-reduced-motion`.** The gradient stops animating and the beam is
 *    drawn as a plain static path. (§8.5 bans `prefers-color-scheme`; reduced motion is a
 *    different media feature and is allowed.)
 * 5. **`orientation`.** The registry sweeps its gradient along x and only along x, because
 *    every beam in its demo is horizontal. Sweeping x across a *vertical* beam changes the
 *    colour of the whole segment at once — it pulses in place instead of travelling. The
 *    vertical mode sweeps y instead. Coordinates stay relative to the container, not to the
 *    individual beam, so a column of beams reads as one pulse moving down the chain rather
 *    than six beams blinking in unison. `"horizontal"` is the default and is the registry's
 *    exact behaviour.
 */
export interface AnimatedBeamProps {
  className?: string
  containerRef: RefObject<HTMLElement | null> // Container ref
  fromRef: RefObject<HTMLElement | null>
  toRef: RefObject<HTMLElement | null>
  curvature?: number
  reverse?: boolean
  /** Which axis the gradient sweeps along. See note 5 above. */
  orientation?: "horizontal" | "vertical"
  pathColor?: string
  pathWidth?: number
  pathOpacity?: number
  gradientStartColor?: string
  gradientStopColor?: string
  delay?: number
  duration?: number
  repeat?: number
  repeatDelay?: number
  startXOffset?: number
  startYOffset?: number
  endXOffset?: number
  endYOffset?: number
}

export const AnimatedBeam: React.FC<AnimatedBeamProps> = ({
  className,
  containerRef,
  fromRef,
  toRef,
  curvature = 0,
  reverse = false, // Include the reverse prop
  orientation = "horizontal",
  duration = 5,
  delay = 0,
  // The theme's own tokens (app/globals.css) rather than the registry's grey and its
  // orange-to-purple gradient.
  pathColor = "var(--border)",
  pathWidth = 2,
  pathOpacity = 1,
  gradientStartColor = "var(--primary)",
  gradientStopColor = "var(--primary)",
  repeat = Infinity,
  repeatDelay = 0,
  startXOffset = 0,
  startYOffset = 0,
  endXOffset = 0,
  endYOffset = 0,
}) => {
  const id = useId()
  const [pathD, setPathD] = useState("")
  const [svgDimensions, setSvgDimensions] = useState({ width: 0, height: 0 })

  // `useReducedMotion()` reads the media query eagerly, so on a reduced-motion machine it
  // returns `true` on the client's *first* render while the prerendered HTML — built with no
  // preference at all — contains the animated gradient. Branching the tree on it directly is
  // therefore a hydration mismatch (React #418) on exactly the machines this branch is for,
  // and it was one until this line existed. Gating on a mounted flag makes the first client
  // render identical to the server's, and the static version takes over an effect later.
  const prefersReducedMotion = useReducedMotion()
  const [mounted, setMounted] = useState(false)
  useEffect(() => setMounted(true), [])
  const isStatic = mounted && prefersReducedMotion === true

  // Calculate the gradient coordinates based on the orientation and reverse props. The
  // sweeping axis runs 10% → 110% (or the mirror of that when reversed); the other axis is
  // pinned, which is what makes the gradient vector parallel to the beam.
  const sweep = reverse
    ? { lead: ["90%", "-10%"], trail: ["100%", "0%"] }
    : { lead: ["10%", "110%"], trail: ["0%", "100%"] }
  const pinned = ["0%", "0%"]

  const gradientCoordinates =
    orientation === "vertical"
      ? { x1: pinned, x2: pinned, y1: sweep.lead, y2: sweep.trail }
      : { x1: sweep.lead, x2: sweep.trail, y1: pinned, y2: pinned }

  // Held in a ref so the observers below can call the latest measurement without being torn
  // down and rebuilt every time an offset prop changes identity.
  const updatePath = useCallback(() => {
    if (!containerRef.current || !fromRef.current || !toRef.current) return

    const containerRect = containerRef.current.getBoundingClientRect()
    const rectA = fromRef.current.getBoundingClientRect()
    const rectB = toRef.current.getBoundingClientRect()

    // LOCAL EDIT: upstream calls `setSvgDimensions` with a fresh object on every
    // measurement, which is never `===` the previous one and so re-renders the beam on every
    // scroll-adjacent resize event even when nothing moved. Bailing out when the numbers are
    // unchanged keeps a burst of resize events to one render.
    const svgWidth = containerRect.width
    const svgHeight = containerRect.height
    setSvgDimensions((current) =>
      current.width === svgWidth && current.height === svgHeight
        ? current
        : { width: svgWidth, height: svgHeight }
    )

    const startX = rectA.left - containerRect.left + rectA.width / 2 + startXOffset
    const startY = rectA.top - containerRect.top + rectA.height / 2 + startYOffset
    const endX = rectB.left - containerRect.left + rectB.width / 2 + endXOffset
    const endY = rectB.top - containerRect.top + rectB.height / 2 + endYOffset

    const controlY = startY - curvature
    const d = `M ${startX},${startY} Q ${(startX + endX) / 2},${controlY} ${endX},${endY}`
    setPathD(d)
  }, [
    containerRef,
    fromRef,
    toRef,
    curvature,
    startXOffset,
    startYOffset,
    endXOffset,
    endYOffset,
  ])

  // The latest measurement, reachable from observer callbacks that are set up once. Assigned
  // in an effect rather than during render: writing a ref while rendering is a side effect,
  // and a render React discards (StrictMode's double pass, a suspended tree) would leave the
  // ref holding a callback built from props that never committed.
  const updatePathRef = useRef(updatePath)
  useEffect(() => {
    updatePathRef.current = updatePath
  }, [updatePath])

  useEffect(() => {
    const measure = () => updatePathRef.current()

    // Observe every element this component measures. The container alone is not enough:
    // a node can move without the container's box changing at all.
    const resizeObserver = new ResizeObserver(measure)
    for (const element of [containerRef.current, fromRef.current, toRef.current]) {
      if (element) resizeObserver.observe(element)
    }

    // A node can also move because something *outside* this container changed height. That
    // is what the window `resize` listener and the font hook below are for. A
    // `MutationObserver` on the container is *not*: a change above the container is not a
    // mutation inside it, so it would not fire for the case it looks like it covers — while
    // six beams each watching the same subtree, each calling `setState`, is a feedback path
    // waiting for the first consumer whose children are not static.
    window.addEventListener("resize", measure)

    // The font swap moves every label. `document.fonts.ready` resolves once it has landed.
    let cancelled = false
    void document.fonts?.ready.then(() => {
      if (!cancelled) measure()
    })

    measure()

    return () => {
      cancelled = true
      resizeObserver.disconnect()
      window.removeEventListener("resize", measure)
    }
  }, [containerRef, fromRef, toRef])

  return (
    <svg
      fill="none"
      width={svgDimensions.width}
      height={svgDimensions.height}
      xmlns="http://www.w3.org/2000/svg"
      className={cn(
        // LOCAL EDIT: the registry also carries `stroke-2` here. That is CSS, and CSS beats
        // the `stroke-width` presentation attribute the paths below set from `pathWidth` —
        // so upstream's `pathWidth` prop silently does nothing at any value but 2. Dropping
        // the utility is what makes the prop real.
        "pointer-events-none absolute top-0 left-0 transform-gpu",
        className
      )}
      viewBox={`0 0 ${svgDimensions.width} ${svgDimensions.height}`}
      aria-hidden="true"
    >
      <path
        d={pathD}
        stroke={pathColor}
        strokeWidth={pathWidth}
        strokeOpacity={pathOpacity}
        strokeLinecap="round"
      />
      {isStatic ? null : (
        <>
          <path
            d={pathD}
            strokeWidth={pathWidth}
            stroke={`url(#${id})`}
            strokeOpacity="1"
            strokeLinecap="round"
          />
          <defs>
            <motion.linearGradient
              className="transform-gpu"
              id={id}
              gradientUnits={"userSpaceOnUse"}
              initial={{
                x1: "0%",
                x2: "0%",
                y1: "0%",
                y2: "0%",
              }}
              animate={{
                x1: gradientCoordinates.x1,
                x2: gradientCoordinates.x2,
                y1: gradientCoordinates.y1,
                y2: gradientCoordinates.y2,
              }}
              transition={{
                delay,
                duration,
                ease: [0.16, 1, 0.3, 1], // https://easings.net/#easeOutExpo
                repeat,
                repeatDelay,
              }}
            >
              <stop stopColor={gradientStartColor} stopOpacity="0"></stop>
              <stop stopColor={gradientStartColor}></stop>
              <stop offset="32.5%" stopColor={gradientStopColor}></stop>
              <stop offset="100%" stopColor={gradientStopColor} stopOpacity="0"></stop>
            </motion.linearGradient>
          </defs>
        </>
      )}
    </svg>
  )
}
