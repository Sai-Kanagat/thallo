import "./globals.css";

import type { Metadata, Viewport } from "next";
import { connection } from "next/server";
import type { ReactNode } from "react";

import { Shell } from "@/components/Shell";

export const metadata: Metadata = {
  title: "Thallo",
  description: "Grown, not generated.",
  manifest: "/manifest.webmanifest",
  icons: { icon: "/icon.svg" },
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f3efe6" },
    { media: "(prefers-color-scheme: dark)", color: "#161a17" },
  ],
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  await connection(); // dynamic rendering: every page view gets its own CSP nonce (proxy.ts)
  return (
    <html lang="en">
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
