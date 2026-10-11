import type { Metadata } from "next";
import "@fontsource-variable/inter";
import "./globals.css";

export const metadata: Metadata = {
  title: "SafeAgent: AI support assistant with manager approvals",
  description:
    "An AI assistant that answers customer requests for your support team. Safe requests are handled automatically, and anything risky waits for a manager to approve.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
