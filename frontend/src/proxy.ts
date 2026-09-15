import { NextRequest, NextResponse } from "next/server";

import { SESSION_COOKIE_NAME, tokenSessaoValido } from "@/lib/auth";

// Tudo passa por aqui (convenção `proxy.ts` do Next.js 16 — sucessora de
// `middleware.ts`, renomeada pelo codemod oficial `middleware-to-proxy`),
// exceto os arquivos estáticos do Next e o favicon (não faz sentido, e
// custaria uma verificação de cookie por asset à toa) — /login e
// /api/login|logout se excluem na própria função abaixo, porque o matcher
// não roda condicional.
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};

const ROTAS_PUBLICAS = new Set(["/login", "/api/login", "/api/logout"]);

export async function proxy(request: NextRequest): Promise<NextResponse> {
  const { pathname } = request.nextUrl;
  if (ROTAS_PUBLICAS.has(pathname)) {
    return NextResponse.next();
  }

  const token = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  if (await tokenSessaoValido(token)) {
    return NextResponse.next();
  }

  const url = request.nextUrl.clone();
  url.pathname = "/login";
  url.search = "";
  // preserva pra onde a pessoa tentou ir, pra mandar de volta pra lá
  // depois do login (validado em app/api/login/route.ts — só aceita
  // caminho interno, nunca um redirect pra fora do site).
  url.searchParams.set("next", pathname + request.nextUrl.search);
  return NextResponse.redirect(url);
}
