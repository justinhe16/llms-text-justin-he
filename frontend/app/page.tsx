import { Suspense } from "react";

import { AuthErrorToast } from "@/components/auth/auth-error-toast";
import { DevPasswordSignIn } from "@/components/auth/dev-password-sign-in";
import { LandingAccount } from "@/components/landing/landing-account";
import { LandingForm } from "@/components/landing/landing-form";
import { AnimatedShinyText } from "@/components/magicui/animated-shiny-text";
import { BlurFade } from "@/components/magicui/blur-fade";
import { DotPattern } from "@/components/magicui/dot-pattern";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * `/` — the first screen. A wordmark, a headline, one input, two buttons.
 *
 * **No nav bar, no feature grid, no testimonials, no footer.** That is the requirement, not
 * a style note: this page does one job, which is to convey what the product is and let a
 * signed-in visitor start a crawl in one paste. Anything added here has to displace
 * something. When in doubt, leave it out.
 *
 * A server component holding three client islands. The wordmark and the headline are
 * rendered on the server and are in the static HTML — they are the page's content, and
 * making them wait for hydration would mean the first paint of a "what is this product"
 * page says nothing.
 *
 * Public: `frontend/middleware.ts` protects `/crawls`, not this route. A signed-out visitor
 * gets the same layout with the field disabled, which states the value proposition and the
 * gate at the same time — better than hiding the input, and much better than bouncing
 * someone to a login screen before they know what the product does.
 */
export default function Home() {
  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden px-6">
      {/* Texture, not decoration: low opacity, and masked to fade out well before the edges
          so the whitespace still does the work. */}
      <DotPattern
        width={28}
        height={28}
        className="[mask-image:radial-gradient(460px_circle_at_center,white,transparent)]"
      />

      {/* useSearchParams() — here, and transitively through everything below that can start
          a sign-in — needs a Suspense boundary during static prerendering, or the build
          warns and bails. */}
      <Suspense fallback={null}>
        <AuthErrorToast />
      </Suspense>

      {/* Signed in only, and it renders nothing at all otherwise. */}
      <Suspense fallback={null}>
        <LandingAccount />
      </Suspense>

      <div className="relative z-10 flex w-full max-w-lg flex-col items-center gap-7 text-center">
        <BlurFade delay={0.02}>
          <h1 className="font-mono text-3xl font-medium tracking-tight text-foreground sm:text-4xl">
            llms.txt
          </h1>
        </BlurFade>

        {/* One animated element, not four. The shine is on the sentence that says what the
            product does, and nowhere else. */}
        <BlurFade delay={0.1}>
          <AnimatedShinyText className="max-w-none text-base sm:text-lg">
            Generate llms.txt for any site
          </AnimatedShinyText>
        </BlurFade>

        <Suspense fallback={<LandingFormFallback />}>
          <LandingForm />
        </Suspense>

        {/* Development only — `process.env.NODE_ENV === "production"` is the first line of
            that component, so Next drops it from every deployed build and the page above is
            exactly the four elements the design calls for. GitHub OAuth is disabled in
            supabase/config.toml locally (README.md "Local test user"), so without this there
            is no way to sign in on a laptop at all. */}
        <Suspense fallback={null}>
          <DevPasswordSignIn />
        </Suspense>
      </div>
    </main>
  );
}

/** Holds the field's and the buttons' space so the stack does not jump as they hydrate. */
function LandingFormFallback() {
  return (
    <div aria-hidden="true" className="flex w-full flex-col items-center gap-7">
      <div className="w-full">
        <Skeleton className="h-12 w-full rounded-full" />
        {/* Mirrors the field's always-present live region, so nothing shifts on hydration. */}
        <div className="min-h-6" />
      </div>
      <Skeleton className="h-11 w-72 rounded-full" />
    </div>
  );
}
