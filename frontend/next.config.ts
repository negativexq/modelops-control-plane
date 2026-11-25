import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Keep the dev badge out of the sidebar footer (router status lives there).
  devIndicators: { position: "bottom-right" },
};

export default nextConfig;
