"use client";

import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// The QueryClient is built inside useState(() => ...), never at module scope. Next.js runs
// this module's top level once per server process, not once per request — a module-scope
// `new QueryClient()` would be the same cache object shared across every request that
// process ever handles, which on the server means one signed-in user's fetched websites
// leaking into the response another user's request renders. useState's lazy initializer
// runs exactly once per component instance (i.e. once per render tree, which on the server
// means once per request), which is what actually gives every request its own cache.
export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // A component that read a website 10s ago can re-render without refetching;
            // past that it refetches on the next thing that reads it (a mount, a window
            // refocus if that were on — see below — or the next poll tick).
            staleTime: 10_000,

            // One retry, not react-query's default of three. A `404`/`409`/`422` from
            // this backend is never transient — retrying it three times just delays the
            // error a user is going to see regardless — and the one real transient case
            // (a dropped connection to the BFF proxy) is covered by a single retry
            // without adding several seconds of exponential backoff to every genuine
            // failure.
            retry: 1,

            // Off because polling already covers freshness for the one thing in this app
            // that changes on its own without user action (a run's status —
            // lib/query/polling.ts). Refetching again on every window focus on top of
            // that would just be a second, redundant path to the same data, and it fires
            // constantly for anyone who alt-tabs.
            refetchOnWindowFocus: false,

            // Explicit even though it is `useQuery`'s own default: `refetchInterval`
            // (lib/query/polling.ts's `pollWhileActive`) polls every three seconds while
            // a run is in progress, and that is fine for a visible tab and a real problem
            // for a background one — a run that takes ten minutes would otherwise poll
            // roughly 200 times from a tab nobody is looking at. Written out here, rather
            // than left to whatever react-query's default happens to be today, so the
            // next person to read this file does not have to check upstream to know this
            // is actually enforced.
            refetchIntervalInBackground: false,
          },
        },
      })
  );

  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
