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
    <html lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} bg-app-gradient min-h-screen antialiased`}
      >
        <TooltipProvider>{children}</TooltipProvider>
        <Toaster />
      </body>
    </html>
  );
}
