/** What the server hands a served doc site (app/plainboard/services/docs_public.py). */

export interface NavItem {
  id: number;
  title: string;
  path: string;
  /** How many pages are directly below it. */
  children: number;
}

export interface Nav {
  items: NavItem[];
  total: number;
  offset: number;
}

export interface Near {
  title: string;
  path: string;
}

export interface SiteView {
  name: string;
  slug: string;
  description: string;
  public: boolean;
  /** The site's address: "" at its own domain, /docs/<slug> here. */
  base: string;
  /** Its JSON: /api/public/docs/<slug> */
  api: string;
  language: string;
  /** Its own language first, then the further ones. */
  languages: string[];
  lang: string;
  lang_base: string;
  project: string;
  updated_at: string;
  home: string | null;
  nav: Nav;
  /** Its AI assistant is on (#119): POST {api}/assistant. */
  assistant?: boolean;
}

export interface PageView {
  id: number;
  title: string;
  path: string;
  lang: string;
  /** Not translated yet: the original is shown. */
  fallback: boolean;
  html: string;
  toc: { level: number; id: string; text: string }[];
  markdown: string;
  description: string;
  updated_at: string;
  breadcrumbs: Near[];
  prev: Near | null;
  next: Near | null;
  /** The top level, then the children of each page on the way down to this one. */
  sidebar: Nav[];
  children: Nav;
}

export interface SearchHit {
  title: string;
  path: string;
  url: string;
  snippet: string;
}

export interface DocsState {
  site: SiteView;
  page: PageView | null;
  /** A private site and nobody signed in: the sign-in. */
  sign_in?: boolean;
}

declare global {
  interface Window {
    __PB_DOCS__?: DocsState;
  }
}
