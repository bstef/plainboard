import { useEffect, useState, useSyncExternalStore } from "react";

/** State persisted in localStorage (JSON). */
export function useStored<T>(key: string, initial: T) {
  const [value, setValue] = useState<T>(() => {
    const raw = localStorage.getItem(key);
    if (raw === null) return initial;
    try {
      return JSON.parse(raw) as T;
    } catch {
      return initial;
    }
  });
  useEffect(() => localStorage.setItem(key, JSON.stringify(value)), [key, value]);
  return [value, setValue] as const;
}

const storedListeners = new Set<() => void>();

/** Like useStored, but every component that reads the key sees a change at once (a setting that one
 * place switches and another one uses). The value is JSON in localStorage, as with useStored. */
export function useSharedStored<T>(key: string, initial: T) {
  const raw = useSyncExternalStore(
    (onChange) => {
      storedListeners.add(onChange);
      window.addEventListener("storage", onChange);
      return () => {
        storedListeners.delete(onChange);
        window.removeEventListener("storage", onChange);
      };
    },
    () => localStorage.getItem(key),
  );
  let value = initial;
  if (raw !== null) {
    try {
      value = JSON.parse(raw) as T;
    } catch {
      /* keep the default */
    }
  }
  const set = (next: T) => {
    localStorage.setItem(key, JSON.stringify(next));
    storedListeners.forEach((notify) => notify());
  };
  return [value, set] as const;
}

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const mql = window.matchMedia(query);
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    },
    () => window.matchMedia(query).matches,
  );
}

/** Desktop layout (sidebar + side chat panel) from Tailwind's `lg` breakpoint. */
export const useIsDesktop = () => useMediaQuery("(min-width: 1024px)");
