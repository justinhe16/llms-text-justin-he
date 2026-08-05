import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";

import "./globals.css";

// Both faces are self-hosted by next/font at build time — the browser makes no
// request to a font CDN at runtime, and the generated size-adjust fallback
// metrics keep the swap from shifting layout.
const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

// Reserved for the llms.txt viewer and anywhere a URL or an id is displayed.
const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "llms-text",
  description: "Generate an llms.txt for any website.",
};

// There is deliberately no global navigation here. The landing page has no
// chrome, and /crawls gets its own minimal header when that ticket lands.
export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // The font variables are declared on <html>, not <body>. Custom properties
    // inherit downward only, and app/globals.css resolves `font-sans` in its
    // base layer on the html element — declaring them any lower would leave
    // that rule pointing at an undefined variable, and every page would
    // silently fall back to the browser's default serif.
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable}`}>
      <body className="bg-app-gradient min-h-screen antialiased">
        <TooltipProvider>{children}</TooltipProvider>
        <Toaster />
      </body>
    </html>
  );
}
