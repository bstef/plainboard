import { useEffect, useSyncExternalStore } from "react";

/** Named themes: each is one block of tokens in index.css. The first dark and the first light one
 * are what "follow the system" picks. */
export const THEMES = [
  { id: "carbon", dark: true, color: "#161614" },
  { id: "stone", dark: false, color: "#f1f0ea" },
] as const;
export type ThemeId = (typeof THEMES)[number]["id"];

const KEY = "plainboard.theme";
const listeners = new Set<() => void>();
const systemDark = () => window.matchMedia("(prefers-color-scheme: dark)");

function stored(): ThemeId | null {
  const value = localStorage.getItem(KEY);
  return THEMES.some((t) => t.id === value) ? (value as ThemeId) : null;
}

/** The theme in use: the chosen one, else the one matching the system. */
export function currentTheme(): ThemeId {
  return stored() ?? THEMES.find((t) => t.dark === systemDark().matches)!.id;
}

function apply() {
  const theme = THEMES.find((t) => t.id === currentTheme())!;
  document.documentElement.dataset.theme = theme.id;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme.color);
  listeners.forEach((notify) => notify());
}

export function setTheme(id: ThemeId) {
  localStorage.setItem(KEY, id);
  apply();
}

/** Current theme plus a setter; follows the system until the user picks one. */
export function useTheme() {
  const theme = useSyncExternalStore((onChange) => {
    listeners.add(onChange);
    return () => listeners.delete(onChange);
  }, currentTheme);
  useEffect(() => {
    const mql = systemDark();
    mql.addEventListener("change", apply);
    return () => mql.removeEventListener("change", apply);
  }, []);
  return [theme, setTheme] as const;
}

apply();
