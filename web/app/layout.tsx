import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "电力现货价格工作台",
    template: "%s｜电力现货价格工作台",
  },
  description: "一键采集全国电力现货价格，查看七日走势、价格区间分布并生成大模型总结。",
  icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
  openGraph: {
    title: "电力现货价格工作台",
    description: "七日走势、价格区间分布与智能总结",
    type: "website",
    locale: "zh_CN",
    images: [{ url: "/og-electricity-workbench.png", width: 1728, height: 910, alt: "电力现货价格工作台" }],
  },
  twitter: {
    card: "summary_large_image",
    title: "电力现货价格工作台",
    description: "七日走势、价格区间分布与智能总结",
    images: ["/og-electricity-workbench.png"],
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
