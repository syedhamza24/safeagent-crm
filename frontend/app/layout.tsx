import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SafeAgent CRM: AI agent with a deterministic safety layer",
  description:
    "An AI support agent that can act on a CRM but cannot act unsafely. Every action is risk-scored and high-risk actions wait for human approval.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
