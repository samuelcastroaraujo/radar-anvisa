import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  // Este projeto já tem CLAUDE.md mantido manualmente na raiz do repo, com
  // convenção própria (uma seção por milestone) — não deixar o Next.js
  // gerar um segundo CLAUDE.md dentro de frontend/, que confundiria isso.
  agentRules: false,
};

export default nextConfig;
