import { NextResponse } from "next/server";

import { ApiError, enviarPergunta } from "@/lib/api";

/**
 * Proxy fino para `POST /chat` no backend FastAPI — roda no servidor do
 * Next.js, então o browser nunca fala com o backend direto (ver
 * `src/lib/api.ts` para a decisão completa).
 */
export async function POST(request: Request) {
  const corpo = (await request.json().catch(() => null)) as { mensagem?: string } | null;
  const mensagem = corpo?.mensagem?.trim();
  if (!mensagem) {
    return NextResponse.json({ detail: "mensagem vazia" }, { status: 422 });
  }

  try {
    const resposta = await enviarPergunta(mensagem);
    return NextResponse.json(resposta);
  } catch (erro) {
    if (erro instanceof ApiError) {
      return NextResponse.json({ detail: erro.message }, { status: erro.status || 502 });
    }
    return NextResponse.json({ detail: "erro inesperado" }, { status: 500 });
  }
}
