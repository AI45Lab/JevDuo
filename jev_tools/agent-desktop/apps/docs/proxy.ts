import { createProxy } from "@vercel/geistdocs/proxy";
import { NextResponse, type NextFetchEvent, type NextRequest } from "next/server";
import { config as geistdocsConfig } from "@/lib/geistdocs/config";
import { source } from "@/lib/geistdocs/source";

const proxy = createProxy({
  config: geistdocsConfig,
  markdownRoutes: [{ from: "/desktop/*path", to: "/[lang]/desktop/llms.mdx/*path" }],
  before: ({ request }) => {
    const pathname = request.nextUrl.pathname;
    if (pathname === "/desktop/sitemap.xml" || pathname === "/desktop/robots.txt") {
      return NextResponse.next();
    }
    if (
      pathname === "/desktop/sitemap.md" ||
      pathname === "/desktop/llms.txt" ||
      pathname === "/desktop/og" ||
      pathname.startsWith("/desktop/og/")
    ) {
      return NextResponse.rewrite(new URL(`/en${pathname}`, request.url));
    }
    if (pathname === "/cn/desktop" || pathname.startsWith("/cn/desktop/")) {
      return new Response(null, { status: 404 });
    }
    if (
      Object.keys(geistdocsConfig.translations ?? {}).some(
        (lang) => pathname === `/${lang}` || pathname.startsWith(`/${lang}/`)
      )
    ) {
      return new Response(null, { status: 404 });
    }
    return null;
  },
  after: ({ request }) => {
    const pathname = request.nextUrl.pathname;
    const match = pathname.match(/^\/desktop(?:\/(.*))?$/);
    if (!match) return null;
    const [, rawPath = ""] = match;
    const path = rawPath.replace(/\/$/, "");
    if (
      ["llms.txt", "sitemap.md", "sitemap.xml", "robots.txt"].includes(path) ||
      path === "og" || path.startsWith("og/") || path === "llms.mdx" || path.startsWith("llms.mdx/")
    ) return null;
    if (
      !source.getPage(path ? path.split("/") : [], geistdocsConfig.defaultLanguage)
    ) {
      return new Response(null, { status: 404 });
    }
    return null;
  },
});

export const config = {
  matcher: [
    "/((?!vc-ap-[^/]+/|desktop/api(?:/|$)|desktop/favicon.ico|desktop/diagrams/|api(?:/|$)|_next/static|_next/image|favicon.ico|sitemap.xml|robots.txt).*)",
  ],
};

export default async function handleRequest(request: NextRequest, event: NextFetchEvent) {
  const response = await proxy(request, event);
  if (process.env.VERCEL_ENV && process.env.VERCEL_ENV !== "production") {
    response.headers.set("X-Robots-Tag", "noindex, nofollow");
  }
  return response;
}
