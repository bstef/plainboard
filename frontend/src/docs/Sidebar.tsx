import clsx from "clsx";
import { ChevronRight, Loader2 } from "lucide-react";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { pageUrl } from "./api";
import type { Nav, NavItem, SiteView } from "./types";

export const levelKey = (lang: string, parent: number | null) => `${lang}:${parent ?? "top"}`;

export interface TreeProps {
  site: SiteView;
  lang: string;
  levels: Map<string, Nav>;
  expanded: Set<number>;
  loading: Set<string>;
  currentId: number | null;
  onToggle: (item: NavItem) => void;
  onMore: (parent: number | null) => void;
  onNavigate: (href: string) => void;
  onPrefetch: (href: string) => void;
}

/** The site's pages as a tree: each level comes when it is opened, long levels a part at a time, so a site of
 * thousands of pages shows as fast as one of ten. The current page stays in view. */
export function Tree(props: TreeProps) {
  const { t } = useTranslation();
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    const current = ref.current?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!current) return;
    const box = ref.current!.closest("[data-scroll]") as HTMLElement | null;
    if (!box) return;
    const a = current.getBoundingClientRect();
    const b = box.getBoundingClientRect();
    if (a.top < b.top + 8 || a.bottom > b.bottom - 8) box.scrollTop += a.top - b.top - b.height / 3;
  }, [props.currentId]);
  return (
    <nav ref={ref} aria-label={t("docsSite.pages")} className="doc-tree">
      <Level {...props} parent={null} depth={0} />
    </nav>
  );
}

function Level(props: TreeProps & { parent: number | null; depth: number }) {
  const { t } = useTranslation();
  const { site, lang, levels, parent, depth, loading } = props;
  const key = levelKey(lang, parent);
  const level = levels.get(key);
  if (!level) {
    return loading.has(key) ? (
      <div className="doc-tree-wait" style={{ paddingLeft: indent(depth) + 8 }}>
        <Loader2 className="size-3.5 animate-spin" />
      </div>
    ) : null;
  }
  const more = level.total - level.items.length;
  return (
    <ul role={depth ? "group" : "tree"} className={clsx(depth > 0 && "doc-tree-sub")}>
      {level.items.map((item) => (
        <Item key={item.id} {...props} item={item} depth={depth} href={pageUrl(site, lang, item.path)} />
      ))}
      {more > 0 && (
        <li>
          <button type="button" className="doc-tree-more" style={{ paddingLeft: indent(depth) + 30 }} onClick={() => props.onMore(parent)} disabled={loading.has(key)}>
            {loading.has(key) ? <Loader2 className="size-3.5 animate-spin" /> : null}
            {t("docsSite.more", { count: more })}
          </button>
        </li>
      )}
    </ul>
  );
}

/** Deep trees indent less the deeper they go, so a page twelve levels down still has room for its title. */
const indent = (depth: number) => Math.round(Math.min(depth, 4) * 14 + Math.max(0, depth - 4) * 6);

function Item(props: TreeProps & { item: NavItem; depth: number; href: string }) {
  const { t } = useTranslation();
  const { item, depth, href, expanded, currentId } = props;
  const open = expanded.has(item.id);
  const current = currentId === item.id;
  return (
    <li role="treeitem" aria-expanded={item.children ? open : undefined} aria-selected={current}>
      <div className={clsx("doc-tree-row", current && "is-current")} style={{ paddingLeft: indent(depth) }}>
        {item.children > 0 ? (
          <button
            type="button"
            className="doc-tree-toggle"
            aria-label={t(open ? "docsSite.collapse" : "docsSite.expand", { title: item.title })}
            onClick={() => props.onToggle(item)}
          >
            <ChevronRight className={clsx("size-3.5 transition-transform", open && "rotate-90")} />
          </button>
        ) : (
          <span className="doc-tree-dot" aria-hidden />
        )}
        <a
          href={href}
          aria-current={current ? "page" : undefined}
          onPointerEnter={() => props.onPrefetch(href)}
          onFocus={() => props.onPrefetch(href)}
          onClick={(e) => {
            if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
            e.preventDefault();
            props.onNavigate(href);
            // A page with pages below it opens them when it is chosen.
            if (item.children > 0 && !open) props.onToggle(item);
          }}
        >
          {item.title}
        </a>
      </div>
      {open && item.children > 0 && <Level {...props} parent={item.id} depth={depth + 1} />}
    </li>
  );
}
