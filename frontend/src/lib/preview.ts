/** A preview link (`/preview/<port>/…`, `/preview-base/<port>/…`) as the address the same page has
 * inside the agents' environment, where the browser window opens it. Null: not a preview link. */
export function browserAddress(href: string | undefined): string | null {
  if (!href) return null;
  let url: URL;
  try {
    url = new URL(href, window.location.origin);
  } catch {
    return null;
  }
  if (url.origin !== window.location.origin) return null;
  const match = url.pathname.match(/^\/(preview|preview-base)\/(\d+)(\/.*)?$/);
  if (!match) return null;
  const [, kind, port, rest = "/"] = match;
  return `http://localhost:${port}${kind === "preview-base" ? `/preview-base/${port}${rest}` : rest}${url.search}${url.hash}`;
}

/** The other way round: where PlainBoard serves a page of the agents' environment, for a real tab. */
export function previewPath(address: string): string | null {
  let url: URL;
  try {
    url = new URL(address);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(url.hostname) || !url.port) return null;
  const based = url.pathname.startsWith(`/preview-base/${url.port}/`);
  return `${based ? "" : `/preview/${url.port}`}${url.pathname}${url.search}${url.hash}`;
}
