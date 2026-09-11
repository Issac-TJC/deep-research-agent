import type { Metadata } from "next";
import "./style.css";
export const metadata: Metadata = {
  title: "Deep Research · 研究工作台",
  description: "可追溯、可恢复的多 Agent 研究工作台",
};
export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
