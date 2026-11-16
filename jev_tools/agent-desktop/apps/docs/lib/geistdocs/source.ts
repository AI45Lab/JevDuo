import { createSource } from "@vercel/geistdocs/source";
import { docs } from "@/.source/server";
import { config } from "./config";

const templateSource = createSource({
  docs,
  config,
  baseUrl: "/desktop",
  markdown: {
    frontmatter: (defaults) => ({ ...defaults, docs_index: "/desktop/llms.txt" }),
  },
});

export const geistdocsSource = {
  ...templateSource,
  getPageImage: (page: Parameters<typeof templateSource.getPageImage>[0]) => ({
    segments: page.slugs,
    url: `/desktop/og${page.slugs.length ? `/${page.slugs.join("/")}` : ""}`,
  }),
};

export const source = geistdocsSource.source;
export const getPageImage = geistdocsSource.getPageImage;
export const getLLMText = geistdocsSource.getPageMarkdown;
