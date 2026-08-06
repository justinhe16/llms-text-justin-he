"use client";

import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import Link from "next/link";
import { ArrowRight, LoaderCircle } from "lucide-react";

import { Input } from "@/components/ui/input";
import type { AddSiteError } from "@/lib/landing/use-add-site";

type SiteUrlFieldProps = {
  /** Whether the visitor is signed in. `false` disables the field and arms the sign-in
   *  overlay; the field is never merely decorative. */
  isSignedIn: boolean;
  /** True until `useUser()` has resolved. Held disabled but *without* the sign-in placeholder
   *  or the overlay, so a signed-in visitor never sees "Sign in to add a site" flash. */
  isAuthLoading: boolean;
  /** True across the whole create-then-trigger sequence: spinner in, arrow out. */
  isBusy: boolean;
  error: AddSiteError | null;
  onSubmit: (value: string) => void;
  onClearError: () => void;
  onRetry: () => void;
  onSignIn: () => void;
};

/**
 * The one input on the landing page: a URL, an arrow, and the message that appears under it
 * when something goes wrong.
 *
 * ## The signed-out state is a real `disabled` input, plus a button on top of it
 *
 * The field is genuinely `disabled` — assistive technology is told so, and the design
 * system's `disabled:` styling applies — which also means `disabled:pointer-events-none`
 * swallows every click on it. That is why the transparent button sits above it rather than
 * an `onClick` sitting on the input: the natural first action on this page is to click the
 * field, and doing nothing is a dead end. The overlay is a real, focusable, labelled button,
 * so the keyboard path to sign-in is the same as the mouse path.
 *
 * Nothing here is themed with a `dark:` variant (CLAUDE.md rule 7); the disabled and focus
 * treatments both come from `components/ui/input.tsx`'s own tokens.
 */
export function SiteUrlField({
  isSignedIn,
  isAuthLoading,
  isBusy,
  error,
  onSubmit,
  onClearError,
  onRetry,
  onSignIn,
}: SiteUrlFieldProps) {
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const hasAutoFocused = useRef(false);
  const messageId = useId();

  // "Input enabled and focused on load." The focus cannot be a plain `autoFocus`: at mount
  // `useUser()` is still resolving and the input is disabled, and a disabled input cannot
  // take focus. This runs the moment it becomes enabled, once — never stealing focus back
  // from a user who has since clicked something else.
  useEffect(() => {
    if (hasAutoFocused.current || !isSignedIn) return;
    hasAutoFocused.current = true;
    inputRef.current?.focus();
  }, [isSignedIn]);

  const isDisabled = !isSignedIn || isBusy || isAuthLoading;

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isDisabled) return;
    onSubmit(value);
  }

  return (
    // `noValidate` is load-bearing, not boilerplate. `type="url"` below is there for the
    // mobile keyboard and for autofill, but it also arms the browser's own constraint
    // validation — which would intercept the submit of a bare "example.com" and show a
    // native bubble instead of letting `parseSiteUrl` put a written message under the
    // field. The ticket asks for the message; this is what lets it through.
    <form onSubmit={handleSubmit} noValidate className="w-full">
      <div className="relative">
        <Input
          ref={inputRef}
          type="url"
          inputMode="url"
          autoComplete="url"
          spellCheck={false}
          name="url"
          aria-label="Website URL"
          aria-invalid={error !== null}
          aria-describedby={error === null ? undefined : messageId}
          disabled={isDisabled}
          placeholder={isSignedIn || isAuthLoading ? "https://example.com" : "Sign in to add a site"}
          value={value}
          onChange={(event) => {
            setValue(event.target.value);
            if (error) onClearError();
          }}
          className="h-12 rounded-full pr-12 pl-5 font-mono text-sm shadow-none"
        />

        <button
          type="submit"
          // Deliberately not disabled on an empty field. A control that silently does
          // nothing is the dead end this page is trying to avoid; pressing it with nothing
          // typed produces "Enter a website URL." under the field, which is an answer.
          disabled={isDisabled}
          aria-label={isBusy ? "Adding site" : "Add site"}
          className="absolute top-1/2 right-1.5 flex size-9 -translate-y-1/2 cursor-pointer items-center justify-center rounded-full text-muted-foreground transition-colors outline-none hover:bg-muted hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-40"
        >
          {isBusy ? (
            <LoaderCircle className="size-4 animate-spin" aria-hidden="true" />
          ) : (
            <ArrowRight className="size-4" aria-hidden="true" />
          )}
        </button>

        {/* Signed out only. Covers the field and the arrow both, so every click anywhere on
            the control starts sign-in rather than some of them doing nothing. */}
        {!isSignedIn && !isAuthLoading ? (
          <button
            type="button"
            onClick={onSignIn}
            className="absolute inset-0 cursor-pointer rounded-full outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            <span className="sr-only">Sign in to add a site</span>
          </button>
        ) : null}
      </div>

      {/* A live region that is always present, so a message appearing in it is announced.
          Rendering the region itself conditionally would leave nothing for a screen reader
          to observe a change in. */}
      <div aria-live="polite" className="min-h-6">
        {error ? (
          <p id={messageId} className="mt-2 px-1 text-left text-sm text-destructive">
            {error.message}
            {error.retryable ? (
              <>
                {" "}
                <button
                  type="button"
                  onClick={onRetry}
                  className="cursor-pointer rounded-sm font-medium text-foreground underline underline-offset-4 outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
                >
                  Try again
                </button>
              </>
            ) : null}
            {/* The site exists even though its run does not, so the URL the user typed is no
                longer addable — without this link the only way back to it would be to paste
                the same URL again and rely on the 409. */}
            {error.websiteId ? (
              <>
                {" "}
                <Link
                  href={`/crawls/${error.websiteId}`}
                  className="cursor-pointer rounded-sm font-medium text-foreground underline underline-offset-4 outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
                >
                  Open the site
                </Link>
              </>
            ) : null}
          </p>
        ) : null}
      </div>
    </form>
  );
}
