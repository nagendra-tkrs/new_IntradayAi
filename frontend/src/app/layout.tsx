import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "IntradayAI - Stock Analysis & Paper Trading",
  description: "AI-powered intraday stock analysis and paper trading for Indian markets (NSE/BSE)",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-[#0a0e17] text-gray-200 antialiased">
        {children}
      </body>
    </html>
  );
}
