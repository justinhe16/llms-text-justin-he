"use client";

// The Schedule tab's write path: what the controls currently show, when that becomes a
// request, and what happens when the request fails. All of it lives here rather than in the
// panel because none of it is markup (ARCHITECTURE.md §8.4).
//
// ## Why there is a draft at all, rather than React Query's `onMutate`
//
// The optimistic-update pattern React Query documents puts the optimistic write in
// `onMutate`, which fires when the mutation starts. That is the wrong instant for this
// screen. The ticket asks for both a ~500ms debounce (so dragging down four radio options is
// one request, not four) *and* a toggle that does not lag behind the click — and `onMutate`
// cannot satisfy both at once, because with a debounce the mutation does not start until
// 500ms after the click. An `onMutate`-based optimistic update would leave the switch sitting
// in its old position for exactly the half-second the ticket calls out as feeling broken.
//
// So the optimism lives one level earlier: a *draft* is applied the moment a control is
// touched, and the request is what gets debounced behind it. Rollback is the same idea in
// reverse — dropping the draft re-reveals the server's value underneath, with no snapshot to
// restore, because the draft never overwrote anything.
//
// ## One draft, one request
//
// `UpsertScheduleRequest` has no partial update: every write carries both `active` and
// `interval_minutes`. Two independently debounced controls would therefore race, and the
// later timer would overwrite the other control's change with the value it captured before
// that change happened ("I turned it on, then picked Weekly, and now it's off again"). One
// draft object holding both fields, and one timer over that object, makes that race
// unrepresentable rather than merely unlikely.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  Schedule,
  ScheduleIntervalMinutes,
  UpsertScheduleRequest,
} from "@/lib/api/schedules";
import { usePutSchedule } from "@/lib/query/use-put-schedule";

import {
  DEFAULT_SCHEDULE_INTERVAL_MINUTES,
  isScheduleIntervalMinutes,
} from "./schedule-presets";

/** Long enough to coalesce a walk through the radio group, short enough that a user who
 * changes one thing and looks away sees it saved. The ticket's number. */
export const SCHEDULE_SAVE_DEBOUNCE_MS = 500;

export interface ScheduleDraft {
  active: boolean;
  /** `null` when the stored interval is not one of the four presets — see
   * `isScheduleIntervalMinutes`. The picker then shows nothing selected rather than claiming
   * a preset that is not what is stored. */
  intervalMinutes: ScheduleIntervalMinutes | null;
}

/**
 * The server's state in the shape the controls bind to.
 *
 * `null` — no schedule row at all — is not an empty form: it is "off, with daily
 * preselected", which is what makes enabling a brand-new schedule a single click instead of
 * a pick-then-enable two-step.
 */
export function scheduleToDraft(schedule: Schedule | null): ScheduleDraft {
  if (schedule === null) {
    return { active: false, intervalMinutes: DEFAULT_SCHEDULE_INTERVAL_MINUTES };
  }

  return {
    active: schedule.active,
    intervalMinutes: isScheduleIntervalMinutes(schedule.interval_minutes)
      ? schedule.interval_minutes
      : null,
  };
}

/** A draft is only sendable once an interval is chosen — `UpsertScheduleRequest` requires
 * one, and there is no honest default to substitute for a stored custom value. */
function draftToBody(draft: ScheduleDraft): UpsertScheduleRequest | null {
  if (draft.intervalMinutes === null) return null;
  return { active: draft.active, interval_minutes: draft.intervalMinutes };
}

function sameBody(a: UpsertScheduleRequest | null, b: UpsertScheduleRequest | null): boolean {
  if (a === null || b === null) return a === b;
  return a.active === b.active && a.interval_minutes === b.interval_minutes;
}

export interface ScheduleEditor {
  /** What the controls render: the draft while one is outstanding, the server's value
   * otherwise. */
  value: ScheduleDraft;
  /** True from the moment a control is touched until the server has confirmed or rejected
   * the change. Drives the inline "Saving…" indicator — not a disabled state, since blocking
   * the panel during its own debounce window is the opposite of what the debounce is for. */
  isSaving: boolean;
  /** True when the stored interval is not one of the presets, so there is nothing valid to
   * write until one is picked. */
  needsIntervalChoice: boolean;
  setActive: (active: boolean) => void;
  setIntervalMinutes: (minutes: ScheduleIntervalMinutes) => void;
}

export function useScheduleEditor({
  websiteId,
  schedule,
}: {
  websiteId: string;
  /** `undefined` while the query is still loading — treated as "no schedule" for shaping
   * purposes; the panel does not render controls in that state anyway. */
  schedule: Schedule | null | undefined;
}): ScheduleEditor {
  const serverValue = useMemo(() => scheduleToDraft(schedule ?? null), [schedule]);
  const serverBody = useMemo(() => draftToBody(serverValue), [serverValue]);

  const [draft, setDraft] = useState<ScheduleDraft | null>(null);

  // The body of the most recent request actually sent. Every settle handler checks against
  // it, so a slower earlier response cannot clear or roll back a draft that has since moved
  // on. Two requests really can overlap: the debounce timer is per-change, not a lock, so a
  // change made while a request is in flight starts a second one.
  const latestSentBody = useRef<UpsertScheduleRequest | null>(null);

  // A change that has been drafted but whose timer has not fired yet. Held in a ref purely so
  // the unmount effect below can see it without re-subscribing on every keystroke.
  const unsentBody = useRef<UpsertScheduleRequest | null>(null);

  const { mutate, isPending } = usePutSchedule({
    onSuccess: (_data, variables) => {
      // `usePutSchedule` has already written the response to the cache and invalidated, so
      // `serverValue` is about to equal what this draft was asking for. Dropping the draft
      // here rather than diffing avoids the panel getting stuck showing a draft forever if
      // the server ever answers with something other than what was sent.
      if (sameBody(variables.body, latestSentBody.current)) setDraft(null);
    },
    onError: (_error, variables) => {
      // The toast is already handled inside `usePutSchedule` from `ApiError.message`, so a
      // non-owner's `403` reads as the backend's own wording. All that is left here is the
      // revert: drop the draft and the server's last known state shows through again, which
      // is the ticket's "never show a setting that didn't persist".
      if (sameBody(variables.body, latestSentBody.current)) setDraft(null);
    },
  });

  const send = useCallback(
    (body: UpsertScheduleRequest) => {
      latestSentBody.current = body;
      unsentBody.current = null;
      mutate({ websiteId, body });
    },
    [mutate, websiteId]
  );

  useEffect(() => {
    if (draft === null) return;

    const body = draftToBody(draft);
    if (body === null) return;

    // Back where we started — toggled on and off again inside the window, say. There is
    // nothing to persist, and firing a no-op `PUT` would still bump `updated_at` and
    // invalidate every mounted websites list for a change nobody made.
    if (sameBody(body, serverBody)) {
      unsentBody.current = null;
      setDraft(null);
      return;
    }

    // This effect re-runs whenever `serverValue` changes, which includes background refetches
    // landing while a request is in flight. Without this guard that would queue a duplicate
    // of the request already on the wire.
    if (isPending && sameBody(body, latestSentBody.current)) return;

    unsentBody.current = body;
    const timer = setTimeout(() => send(body), SCHEDULE_SAVE_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [draft, serverBody, isPending, send]);

  // Radix Tabs unmounts the inactive panel, so switching to Runs inside the debounce window
  // would otherwise drop a change the user already saw applied. `unsentBody` is non-null for
  // exactly the span between drafting and sending, so this flushes that case and no other —
  // in particular it does not re-send a request that already went out.
  useEffect(() => {
    return () => {
      const pending = unsentBody.current;
      if (pending !== null) send(pending);
    };
  }, [send]);

  const setActive = useCallback(
    (active: boolean) => setDraft((current) => ({ ...(current ?? serverValue), active })),
    [serverValue]
  );

  const setIntervalMinutes = useCallback(
    (minutes: ScheduleIntervalMinutes) =>
      setDraft((current) => ({ ...(current ?? serverValue), intervalMinutes: minutes })),
    [serverValue]
  );

  const value = draft ?? serverValue;

  return {
    value,
    isSaving: draft !== null,
    needsIntervalChoice: value.intervalMinutes === null,
    setActive,
    setIntervalMinutes,
  };
}
