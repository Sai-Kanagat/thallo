import type { NextConfig } from "next";

// The browser only ever talks to this origin. /api is forwarded server side to the FastAPI app,
// so the session cookie stays first party and SameSite=Strict holds.
const apiOrigin = process.env.THALLO_API_ORIGIN ?? "http://127.0.0.1:8000";

const security = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
  { key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" },
];

const config: NextConfig = {
  poweredByHeader: false,
  reactStrictMode: true,
  async headers() {
    // The voice page records answers, so only it may ask for the microphone; it comes last and wins.
    const voice = security.map((h) =>
      h.key === "Permissions-Policy"
        ? { ...h, value: "camera=(), microphone=(self), geolocation=(), payment=(), usb=()" }
        : h,
    );
    return [
      { source: "/:path*", headers: security },
      { source: "/voice", headers: voice },
    ];
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
};

export default config;
