import type { Metadata } from "next";
import DashboardClient from "./DashboardClient";

export const metadata: Metadata = {
  title: "电力现货价格工作台",
  description: "一键采集、七日走势、价格区间分布与大模型分析",
};

export default function Home() {
  return <DashboardClient />;
}
