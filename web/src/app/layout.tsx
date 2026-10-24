import type { Metadata } from "next";
import { Inter } from "next/font/google";
import { BRAND, UNIVERSE } from "@/config/site";
import "./globals.css";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });

export const metadata: Metadata = {
  title: { default: BRAND, template: `%s | ${BRAND}` },
  description: `Factor scores for ${UNIVERSE} stocks, with the reasons behind each one and a history of how it changed.`,
};

// Each page renders its own <AppShell>, because the freshness card shows the
// dates of what that page displays and a layout can't see page data.
export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en-IN" className={inter.variable}>
      <body>{children}</body>
    </html>
  );
}
