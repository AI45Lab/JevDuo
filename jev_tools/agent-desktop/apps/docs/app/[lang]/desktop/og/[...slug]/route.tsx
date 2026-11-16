import type { NextRequest } from "next/server";
import { source } from "@/lib/geistdocs/source";
import { renderOgImage } from "../og-image";

export const GET = async (
  _request: NextRequest,
  { params }: RouteContext<"/[lang]/desktop/og/[...slug]">
) => {
  const { slug, lang } = await params;
  const pageSlugs = slug.at(-1) === "image.png" ? slug.slice(0, -1) : slug;
  const page = source.getPage(pageSlugs, lang);

  if (!page) {
    return new Response("Not found", { status: 404 });
  }

  return renderOgImage(
    page.slugs.length
      ? (page.data.title ?? "agent-desktop")
      : "Desktop\nAutomation for AI"
  );
};

export const generateStaticParams = async ({
  params,
}: RouteContext<"/[lang]/desktop/og/[...slug]">) => {
  const { lang } = await params;

  return source.getPages(lang).flatMap((page) => {
    const image = { lang: page.locale, slug: [...page.slugs, "image.png"] };
    return page.slugs.length
      ? [image, { lang: page.locale, slug: page.slugs }]
      : [image];
  });
};
