import { defineConfig } from "@vercel/geistdocs/config";
import {
  ai,
  basePath,
  github,
  Logo,
  nav,
  navbarBrand,
  prompt,
  siteId,
  suggestions,
  title,
  translations,
} from "@/geistdocs";

export const config = defineConfig({
  title,
  agent: { enabled: false },
  defaultLanguage: "en",
  logo: <Logo />,
  logoHref: "/desktop",
  github,
  nav,
  navbarBrand,
  basePath,
  siteId,
  siteUrl: "https://agent-browser.dev",
  translations,
  webmcp: { enabled: false },
  language: { enabled: false },
  feedback: { enabled: false },
  content: [{ id: "docs", label: "Docs", dir: "content/docs", route: "/desktop" }],
  ai: {
    endpoint: "/desktop/api/chat",
    prompt,
    suggestions,
    ...ai,
  },
});
