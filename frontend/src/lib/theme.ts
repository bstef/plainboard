import { useEffect, useSyncExternalStore } from "react";

/** A design is a look: its type, corners, surfaces and how the parts are drawn. Fahrplan lives in
 * index.css, every other design is one stylesheet in src/designs. The first one listed is what a
 * browser gets that has not chosen: Classic, whose colour makes the parts easier to tell apart.
 * Each design has a dark and a
 * light theme (a block of colour tokens); `color` is the browser's toolbar colour for it and
 * `accent` the colour of its strong element, for the preview in the picker.
 * To add a design: write its stylesheet, import it in main.tsx, list it here and name it in
 * i18n (`look.design.<id>`). */
export const DESIGNS = [
  { id: "classic", dark: { theme: "classic-dark", color: "#18181b", accent: "#5b54ea" }, light: { theme: "classic-light", color: "#ffffff", accent: "#4f46e5" } },
  { id: "fahrplan", dark: { theme: "carbon", color: "#161614", accent: "#ecebe3" }, light: { theme: "stone", color: "#f1f0ea", accent: "#1b1a17" } },
] as const;
export type DesignId = (typeof DESIGNS)[number]["id"];

/** Light, dark, or whatever the system says. */
export const APPEARANCES = ["system", "light", "dark"] as const;
export type Appearance = (typeof APPEARANCES)[number];

export interface Look {
  design: DesignId;
  appearance: Appearance;
  /** What the appearance comes down to right now. */
  dark: boolean;
}

const DESIGN_KEY = "plainboard.design";
const APPEARANCE_KEY = "plainboard.appearance";
const LEGACY_THEME_KEY = "plainboard.theme"; // "carbon" | "stone", from before designs existed
const listeners = new Set<() => void>();
const systemDark = () => window.matchMedia("(prefers-color-scheme: dark)");

function storedDesign(): DesignId {
  const value = localStorage.getItem(DESIGN_KEY);
  return DESIGNS.some((d) => d.id === value) ? (value as DesignId) : DESIGNS[0].id;
}

function storedAppearance(): Appearance {
  const value = localStorage.getItem(APPEARANCE_KEY);
  if (APPEARANCES.includes(value as Appearance)) return value as Appearance;
  const legacy = localStorage.getItem(LEGACY_THEME_KEY);
  return legacy === "carbon" ? "dark" : legacy === "stone" ? "light" : "system";
}

let look: Look = read();

function read(): Look {
  const appearance = storedAppearance();
  return { design: storedDesign(), appearance, dark: appearance === "system" ? systemDark().matches : appearance === "dark" };
}

function apply() {
  look = read();
  const design = DESIGNS.find((d) => d.id === look.design)!;
  const theme = look.dark ? design.dark : design.light;
  const root = document.documentElement;
  root.dataset.design = design.id;
  root.dataset.theme = theme.theme;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme.color);
  listeners.forEach((notify) => notify());
}

export function setDesign(id: DesignId) {
  localStorage.setItem(DESIGN_KEY, id);
  apply();
}

export function setAppearance(appearance: Appearance) {
  localStorage.setItem(APPEARANCE_KEY, appearance);
  apply();
}

/** The look in use; changes when the user picks another or the system switches between light and dark. */
export function useLook(): Look {
  const current = useSyncExternalStore(
    (onChange) => {
      listeners.add(onChange);
      return () => listeners.delete(onChange);
    },
    () => look,
  );
  useEffect(() => {
    const mql = systemDark();
    mql.addEventListener("change", apply);
    return () => mql.removeEventListener("change", apply);
  }, []);
  return current;
}

apply();
