import { NextRequest, NextResponse } from "next/server";

import { credenciaisValidas, gerarTokenSessao, SESSION_COOKIE_NAME } from "@/lib/auth";

const SESSAO_MAX_IDADE_SEGUNDOS = 60 * 60 * 24 * 30; // 30 dias

function proximaUrlSegura(bruto: string): string {
  // Só aceita caminho interno começando com "/" e não "//" — "//evil.com"
  // é interpretado pelo browser como um domínio diferente (protocol-
  // relative URL), então rejeitar isso evita um open redirect via `next`.
  if (bruto.startsWith("/") && !bruto.startsWith("//")) {
    return bruto;
  }
  return "/";
}

export async function POST(request: NextRequest): Promise<NextResponse> {
  const form = await request.formData();
  const usuario = String(form.get("usuario") ?? "");
  const senha = String(form.get("senha") ?? "");
  const next = proximaUrlSegura(String(form.get("next") ?? ""));

  if (!credenciaisValidas(usuario, senha)) {
    const url = new URL("/login", request.url);
    url.searchParams.set("erro", "1");
    if (next !== "/") url.searchParams.set("next", next);
    return NextResponse.redirect(url, { status: 303 });
  }

  const resposta = NextResponse.redirect(new URL(next, request.url), { status: 303 });
  resposta.cookies.set(SESSION_COOKIE_NAME, await gerarTokenSessao(), {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: SESSAO_MAX_IDADE_SEGUNDOS,
  });
  return resposta;
}
