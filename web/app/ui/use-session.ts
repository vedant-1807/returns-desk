"use client";

import { useEffect, useMemo, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";

import { saveSession, type Role, type Session } from "@/lib/api";

const KEY = "returns-desk-session";

function subscribe(onChange: () => void) {
  window.addEventListener("storage", onChange);
  return () => window.removeEventListener("storage", onChange);
}

const noSubscription = () => () => undefined;

/**
 * The signed-in session for this tab, read from session storage. Sends the visitor back to
 * the sign-in page when there is none, or when the session belongs to the other screen.
 */
export function useSession(allowed: Role[]): { session: Session | null; signOut: () => void } {
  const router = useRouter();
  // False while rendering on the server and during hydration, true in the browser after.
  const inBrowser = useSyncExternalStore(
    noSubscription,
    () => true,
    () => false,
  );
  const raw = useSyncExternalStore(
    subscribe,
    () => window.sessionStorage.getItem(KEY),
    () => null,
  );
  const session = useMemo(() => (raw ? (JSON.parse(raw) as Session) : null), [raw]);
  const permitted = session !== null && allowed.includes(session.role);

  useEffect(() => {
    if (inBrowser && !permitted) router.replace("/");
  }, [inBrowser, permitted, router]);

  function signOut() {
    saveSession(null);
    router.replace("/");
  }

  return { session: permitted ? session : null, signOut };
}
