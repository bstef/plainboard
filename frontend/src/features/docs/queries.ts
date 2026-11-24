import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { Attachment, DocDomain, DocPage, DocPageItem, DocPages, DocSite, DocTranslation, DocVersion } from "../../api/types";

/** Every key starts with ["docs", project]: one live event of a site or page refreshes what is shown. */
export const dk = {
  all: (pid: number) => ["docs", pid] as const,
  sites: (pid: number) => ["docs", pid, "sites"] as const,
  children: (pid: number, site: number, parent: number | null) => ["docs", pid, "children", site, parent ?? "top"] as const,
  page: (pid: number, id: number) => ["docs", pid, "page", id] as const,
  versions: (pid: number, id: number) => ["docs", pid, "versions", id] as const,
  version: (pid: number, page: number, id: number) => ["docs", pid, "version", page, id] as const,
  translation: (pid: number, page: number, lang: string) => ["docs", pid, "translation", page, lang] as const,
  files: (pid: number, site: number) => ["docs", pid, "files", site] as const,
  domain: (pid: number, site: number) => ["docs", pid, "domain", site] as const,
  find: (pid: number, site: number, q: string) => ["docs", pid, "find", site, q] as const,
};

export const base = (pid: number) => `/projects/${pid}/docs`;

export const useDocSites = (pid: number) => useQuery({ queryKey: dk.sites(pid), queryFn: () => api.get<DocSite[]>(`${base(pid)}/sites`) });

/** The children of a page (null: the top), the first 200; more with `offset`. */
export const useDocChildren = (pid: number, site: number, parent: number | null, enabled = true) =>
  useQuery({
    queryKey: dk.children(pid, site, parent),
    queryFn: () => api.get<DocPages>(`${base(pid)}/sites/${site}/pages?limit=200${parent === null ? "" : `&parent=${parent}`}`),
    enabled,
    staleTime: 10_000,
  });

export const useDocPage = (pid: number, id: number | null) =>
  useQuery({ queryKey: dk.page(pid, id ?? 0), queryFn: () => api.get<DocPage>(`${base(pid)}/pages/${id}`), enabled: id !== null });

export const useDocVersions = (pid: number, page: number, enabled: boolean) =>
  useQuery({ queryKey: dk.versions(pid, page), queryFn: () => api.get<DocVersion[]>(`${base(pid)}/pages/${page}/versions`), enabled });

export const useDocVersion = (pid: number, page: number, id: number | null) =>
  useQuery({
    queryKey: dk.version(pid, page, id ?? 0),
    queryFn: () => api.get<DocVersion>(`${base(pid)}/pages/${page}/versions/${id}`),
    enabled: id !== null,
    staleTime: Infinity, // a version never changes
  });

export const useDocTranslation = (pid: number, page: number, lang: string | null) =>
  useQuery({
    queryKey: dk.translation(pid, page, lang ?? ""),
    queryFn: () => api.get<DocTranslation>(`${base(pid)}/pages/${page}/translations/${lang}`),
    enabled: !!lang,
  });

export const useDocFiles = (pid: number, site: number, enabled = true) =>
  useQuery({ queryKey: dk.files(pid, site), queryFn: () => api.get<Attachment[]>(`${base(pid)}/sites/${site}/files?limit=500`), enabled });

export const useDocDomain = (pid: number, site: number, enabled: boolean) =>
  useQuery({ queryKey: dk.domain(pid, site), queryFn: () => api.get<DocDomain | null>(`${base(pid)}/sites/${site}/domain`), enabled });

export const useFindPages = (pid: number, site: number, q: string) =>
  useQuery({
    queryKey: dk.find(pid, site, q),
    queryFn: () => api.get<DocPageItem[]>(`${base(pid)}/sites/${site}/pages/find?q=${encodeURIComponent(q)}&limit=50`),
    enabled: q.length > 0,
    placeholderData: (prev) => prev,
  });

/** After a change: everything of the project's docs is fetched again (it is small: only what is open). */
export function useDocsRefresh(pid: number) {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: dk.all(pid) });
}

/** The site's address in the browser: its own domain once verified, else here. */
export function siteHref(site: DocSite): string {
  return site.domain && site.domain_verified_at ? `https://${site.domain}/` : site.url;
}
