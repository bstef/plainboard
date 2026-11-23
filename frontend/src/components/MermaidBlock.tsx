import { useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useLook } from "../lib/theme";
import { Lightbox } from "./Lightbox";

type Mermaid = (typeof import("mermaid"))["default"];

// Loaded on the first diagram only: the library is large and most messages have none.
let loading: Promise<Mermaid> | undefined;
const loadMermaid = () => (loading ??= import("mermaid").then((m) => m.default));

let counter = 0;
let configured = "";

/** A CSS colour (oklch, color-mix, …) as #rrggbb, which Mermaid can compute with. */
function hex(color: string, ctx: CanvasRenderingContext2D): string {
  ctx.clearRect(0, 0, 1, 1);
  ctx.fillStyle = "#000";
  ctx.fillStyle = color;
  ctx.fillRect(0, 0, 1, 1);
  const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
  return `#${[r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

/** Mermaid's theme from the design's colour tokens, as they apply where the diagram stands. */
function themeVariables(el: Element, dark: boolean) {
  const style = getComputedStyle(el);
  const ctx = document.createElement("canvas").getContext("2d", { willReadFrequently: true })!;
  const token = (name: string) => hex(style.getPropertyValue(name).trim(), ctx);
  const [bg, sunk, line, line2, fg, fg2, fg3, you, youBg] = ["--bg", "--sunk", "--line", "--line-2", "--fg", "--fg-2", "--fg-3", "--you", "--you-bg"].map(token);
  return {
    darkMode: dark,
    background: bg,
    fontFamily: style.getPropertyValue("--font-sans").trim() || "sans-serif",
    fontSize: "14px",
    primaryColor: sunk,
    primaryTextColor: fg,
    primaryBorderColor: line2,
    secondaryColor: youBg,
    secondaryTextColor: fg,
    secondaryBorderColor: you,
    tertiaryColor: bg,
    tertiaryTextColor: fg,
    tertiaryBorderColor: line,
    lineColor: fg3,
    textColor: fg,
    mainBkg: sunk,
    nodeBorder: line2,
    clusterBkg: bg,
    clusterBorder: line,
    titleColor: fg,
    edgeLabelBackground: bg,
    noteBkgColor: youBg,
    noteTextColor: fg,
    noteBorderColor: you,
    actorBkg: sunk,
    actorBorder: line2,
    actorTextColor: fg,
    actorLineColor: fg3,
    signalColor: fg2,
    signalTextColor: fg,
    labelBoxBkgColor: sunk,
    labelBoxBorderColor: line2,
    labelTextColor: fg,
    loopTextColor: fg,
    activationBkgColor: line,
    activationBorderColor: line2,
    sequenceNumberColor: bg,
  };
}

/** The SVG for the page and for the lightbox. On the page it fills the block's width, but shrinks
 * at most to MIN_SCALE (else its text gets too small): a wider one scrolls in its block. The
 * lightbox's copy has its own size and the design's background. */
const MIN_SCALE = 0.75;
function sized(svg: string, background: string): { inline: string; image: string } {
  const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
  const root = doc.documentElement;
  const [, , w, h] = (root.getAttribute("viewBox") ?? "").split(/[\s,]+/).map(Number);
  if (!w || !h) return { inline: svg, image: svg };
  root.setAttribute("width", String(Math.ceil(w)));
  root.setAttribute("height", String(Math.ceil(h)));
  root.setAttribute("style", `background-color: ${background}`);
  const image = new XMLSerializer().serializeToString(root);
  root.setAttribute("style", `width: 100%; height: auto; max-width: ${Math.ceil(w)}px; min-width: ${Math.ceil(w * MIN_SCALE)}px`);
  return { inline: new XMLSerializer().serializeToString(root), image };
}

type State = { svg: { inline: string; image: string }; error?: undefined } | { svg?: undefined; error: string } | { svg?: undefined; error?: undefined };

/** A ```mermaid block drawn as a diagram in the app's design. Until it is drawn, and when its
 * syntax is broken, it stays a code block (`fallback`), then with the error below. `open`: the block
 * is still being written (streaming), so it is not drawn yet. */
export function MermaidBlock({ code, open, fallback }: { code: string; open: boolean; fallback: ReactNode }) {
  const { t } = useTranslation();
  const look = useLook();
  const anchor = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<State>({});
  const [zoom, setZoom] = useState(false);
  const [showCode, setShowCode] = useState(false);

  useEffect(() => {
    if (open || !anchor.current) return;
    let cancelled = false;
    const vars = themeVariables(anchor.current, look.dark);
    loadMermaid()
      .then(async (mermaid) => {
        const key = JSON.stringify(vars);
        if (configured !== key) {
          mermaid.initialize({
            startOnLoad: false,
            securityLevel: "strict",
            suppressErrorRendering: true,
            htmlLabels: false,
            theme: "base",
            themeVariables: vars,
          });
          configured = key;
        }
        const { svg } = await mermaid.render(`mermaid-${++counter}`, code);
        if (!cancelled) setState({ svg: sized(svg, vars.background) });
      })
      .catch((e: unknown) => {
        // A parse error: "Parse error on line 3:", the line, a marker, "Expecting …, got …".
        const lines = (e instanceof Error ? e.message : String(e)).split("\n").filter((l) => l.trim());
        const message = lines.length > 1 ? `${lines[0]} ${lines[lines.length - 1]}` : (lines[0] ?? "");
        if (!cancelled) setState({ error: message });
      });
    return () => {
      cancelled = true;
    };
  }, [code, open, look.design, look.dark]);

  const toggle = (
    <button type="button" onClick={() => setShowCode((v) => !v)} className="text-xs text-fg-3 hover:text-fg">
      {t(showCode ? "markdown.showDiagram" : "markdown.showCode")}
    </button>
  );

  if (open || !state.svg) {
    return (
      <div ref={anchor}>
        {fallback}
        {!open && state.error && (
          <p className="-mt-1 mb-2 line-clamp-2 text-xs text-del" role="status" title={state.error}>
            {t("markdown.diagramError", { error: state.error })}
          </p>
        )}
      </div>
    );
  }
  if (showCode) {
    return (
      <div ref={anchor} className="my-2">
        {fallback}
        {toggle}
      </div>
    );
  }
  return (
    <div ref={anchor} className="my-2">
      <button
        type="button"
        title={t("markdown.enlargeDiagram")}
        aria-label={t("markdown.enlargeDiagram")}
        onClick={() => setZoom(true)}
        className="scroll-thin block w-full cursor-zoom-in overflow-x-auto rounded-sm bg-bg p-2 ring-1 ring-line ring-inset"
      >
        <div dangerouslySetInnerHTML={{ __html: state.svg.inline }} />
      </button>
      {toggle}
      {zoom && (
        <Lightbox
          images={[{ src: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(state.svg.image)}`, alt: t("markdown.diagram") }]}
          onClose={() => setZoom(false)}
        />
      )}
    </div>
  );
}
