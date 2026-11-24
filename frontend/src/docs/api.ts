import type { Nav, PageView, SearchHit, SiteView } from "./types";

export class NotFoundError extends Error {}

/** The base of the site in a language: its own language at the site's base, every other one below it. */
export const langBase = (site: SiteView, lang: string) => (lang === site.language ? site.base : `${site.base}/${lang}`);

/** The address of a page ("" is the home page). */
export const pageUrl = (site: SiteView, lang: string, path: string) => `${langBase(site, lang)}/${path}`;

/** Which page of the site an address is, or null when it is not one of its pages (a file, another site). */
export function parseUrl(site: SiteView, url: URL): { lang: string; path: string; hash: string } | null {
  if (url.origin !== location.origin) return null;
  let rest = url.pathname;
  if (site.base) {
    if (rest !== site.base && !rest.startsWith(site.base + "/")) return null;
    rest = rest.slice(site.base.length);
  }
  if (rest.startsWith("/api/") || rest.startsWith("/assets/")) return null;
  const parts = rest.split("/").filter(Boolean).map(decodeURIComponent);
  let lang = site.language;
  if (parts.length && parts[0] !== site.language && site.languages.includes(parts[0])) lang = parts.shift()!;
  const path = parts.join("/");
  if (path === "sitemap.xml" || path === "robots.txt") return null;
  return { lang, path, hash: decodeURIComponent(url.hash.slice(1)) };
}

async function getJson<T>(url: string): Promise<T> {
  const resp = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
  if (resp.status === 404) throw new NotFoundError();
  if (resp.status === 401) {
    location.reload(); // signed out meanwhile: the server shows the sign-in
    throw new Error("Sign in");
  }
  if (!resp.ok) throw new Error(resp.status === 429 ? "429" : resp.statusText);
  return (await resp.json()) as T;
}

// A page fetched once (or while it is being fetched) is not fetched again: a link that was pointed at is
// ready when it is clicked. The site changes rarely; a few hundred pages are kept.
const pages = new Map<string, Promise<PageView>>();
const PAGES_KEPT = 300;

export function loadPage(site: SiteView, lang: string, path: string): Promise<PageView> {
  const key = `${lang}\n${path}`;
  let found = pages.get(key);
  if (!found) {
    found = getJson<PageView>(`${site.api}/page?path=${encodeURIComponent(path)}&lang=${encodeURIComponent(lang)}`);
    found.catch(() => pages.delete(key));
    pages.set(key, found);
    if (pages.size > PAGES_KEPT) pages.delete(pages.keys().next().value!);
  }
  return found;
}

/** Seeds the cache with the page the server rendered. */
export function knownPage(page: PageView, path: string) {
  pages.set(`${page.lang}\n${path}`, Promise.resolve(page));
}

export function prefetch(site: SiteView, href: string) {
  const target = parseUrl(site, new URL(href, location.href));
  if (target) loadPage(site, target.lang, target.path).catch(() => undefined);
}

export const loadNav = (site: SiteView, lang: string, parent: number | null, offset = 0) =>
  getJson<Nav>(`${site.api}/nav?lang=${encodeURIComponent(lang)}&offset=${offset}${parent === null ? "" : `&parent=${parent}`}`);

const searches = new Map<string, Promise<SearchHit[]>>();

export function search(site: SiteView, lang: string, query: string): Promise<SearchHit[]> {
  const key = `${lang}\n${query}`;
  let found = searches.get(key);
  if (!found) {
    found = getJson<SearchHit[]>(`${site.api}/search?q=${encodeURIComponent(query)}&lang=${encodeURIComponent(lang)}&limit=12`);
    found.catch(() => searches.delete(key));
    searches.set(key, found);
  }
  return found;
}
