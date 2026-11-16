import { existsSync } from "node:fs";
import { join } from "node:path";
import { createGeistdocs } from "@vercel/geistdocs/next";
import { withMicrofrontends } from "@vercel/microfrontends/next/config";

const withGeistdocs = createGeistdocs();

// In this monorepo, compile @vercel/geistdocs from its workspace source so edits
// to the package hot-reload in the running dev server (no rebuild/restart).
// Turbopack only watches packages listed here; without it, changes to the
// prebuilt dist in node_modules are ignored. Published/scaffolded installs have
// no workspace source, so this is skipped and the prebuilt dist is used as-is.
const inMonorepo = existsSync(
  join(process.cwd(), "../../packages/geistdocs/src")
);
const config = {
  images: {
    remotePatterns: [
      {
        hostname: "h8dxkfmaphn8o0p3.public.blob.vercel-storage.com",
        pathname: "/docs/**",
        protocol: "https",
      },
    ],
  },
  ...(inMonorepo ? { transpilePackages: ["@vercel/geistdocs"] } : {}),
  cacheComponents: true,
  partialPrefetching: true,

  async headers() {
    return process.env.VERCEL_ENV && process.env.VERCEL_ENV !== "production"
      ? [{ source: "/:path*", headers: [{ key: "X-Robots-Tag", value: "noindex, nofollow" }] }]
      : [];
  },

  async redirects() {
    return [{ source: "/docs/desktop/:path*", destination: "/desktop/:path*", permanent: true }];
  },
};

export default withMicrofrontends(await withGeistdocs(config));
