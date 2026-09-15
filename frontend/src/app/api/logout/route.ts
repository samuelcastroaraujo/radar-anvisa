import { NextRequest, NextResponse } from "next/server";

import { SESSION_COOKIE_NAME } from "@/lib/auth";

export async function POST(request: NextRequest): Promise<NextResponse> {
  const resposta = NextResponse.redirect(new URL("/login", request.url), { status: 303 });
  resposta.cookies.delete(SESSION_COOKIE_NAME);
  return resposta;
}
