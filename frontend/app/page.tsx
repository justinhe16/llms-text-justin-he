import { AnimatedShinyText } from "@/components/magicui/animated-shiny-text";
import { BlurFade } from "@/components/magicui/blur-fade";
import { DotPattern } from "@/components/magicui/dot-pattern";

// Placeholder. The real landing page is a later ticket — this exists so the
// design system has somewhere to prove itself: the warm gradient from
// app/globals.css, Geist Sans and Geist Mono, and Magic UI actually animating.
export default function Home() {
  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden px-6">
      {/* Texture, not decoration: low opacity, and masked to fade out well
          before the edges so the whitespace still does the work. */}
      <DotPattern
        width={28}
        height={28}
        className="[mask-image:radial-gradient(400px_circle_at_center,white,transparent)]"
      />

      <div className="relative flex flex-col items-center gap-6 text-center">
        <BlurFade delay={0.08}>
          <span className="inline-flex items-center rounded-full border border-border bg-card px-3 py-1">
            <AnimatedShinyText className="text-xs tracking-wide">
              Scaffold
            </AnimatedShinyText>
          </span>
        </BlurFade>

        <BlurFade delay={0.16}>
          <h1 className="text-4xl font-medium tracking-tight text-foreground sm:text-5xl">
            llms-text
          </h1>
        </BlurFade>

        <BlurFade delay={0.24}>
          <p className="font-mono text-sm text-muted-foreground">/llms.txt</p>
        </BlurFade>
      </div>
    </main>
  );
}
// PER-146 path-filter proof. Deleted with this branch.
