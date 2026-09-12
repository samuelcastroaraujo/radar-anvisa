/**
 * Cliente do backend FastAPI (RADAR ANVISA) — usado só do lado do
 * servidor (Server Components + Route Handlers), nunca do browser.
 *
 * Decisão de arquitetura do M6: em vez de o browser chamar a API do
 * FastAPI direto (o que exigiria CORS lá — nada configurado até o M5), o
 * Next.js chama server-to-server e o browser só fala com o próprio
 * Next.js (`/api/chat`, ou Server Components que já renderizam com dado
 * pronto). Mais simples e mais seguro (a URL/porta real do backend nunca
 * aparece no bundle do cliente) — o mesmo padrão que vale em produção
 * (Vercel -> Railway).
 */

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError(
      `Não consegui contactar o backend em ${API_BASE_URL}. Ele está no ar?`,
      0,
    );
  }
  if (!resp.ok) {
    const corpo = await resp.text().catch(() => "");
    throw new ApiError(`${resp.status}: ${corpo || resp.statusText}`, resp.status);
  }
  return resp.json() as Promise<T>;
}

export interface NormaCitada {
  id: string;
  tipo_ato: string;
  numero: string;
  ano: number;
  status_vigencia: string;
  url_origem: string;
}

export interface RespostaChat {
  resposta: string;
  fontes: string[];
  normas: NormaCitada[];
}

export async function enviarPergunta(mensagem: string): Promise<RespostaChat> {
  return apiFetch<RespostaChat>("/chat", {
    method: "POST",
    body: JSON.stringify({ mensagem }),
  });
}

export interface ItemTimeline {
  tipo: "norma" | "noticia" | "consulta_publica" | "dou" | string;
  titulo: string;
  data: string;
  url: string | null;
  status_vigencia: string | null;
}

export async function buscarTimeline(dias = 30, limite = 100): Promise<ItemTimeline[]> {
  return apiFetch<ItemTimeline[]>(`/timeline?dias=${dias}&limite=${limite}`, {
    cache: "no-store",
  });
}

export interface ConsultaPublica {
  titulo: string;
  assunto: string | null;
  data_dou: string | null;
  prazo_inicio: string | null;
  prazo_fim: string | null;
  url: string;
  aberta: boolean;
}

export async function buscarConsultasPublicas(
  apenasAbertas = true,
): Promise<ConsultaPublica[]> {
  return apiFetch<ConsultaPublica[]>(
    `/consultas-publicas?apenas_abertas=${apenasAbertas}`,
    { cache: "no-store" },
  );
}

export interface StatusFonte {
  fonte: string;
  status: string | null;
  iniciado_em: string;
  terminado_em: string | null;
  itens_novos: number;
  erro: string | null;
}

export async function buscarStatusFontes(): Promise<StatusFonte[]> {
  return apiFetch<StatusFonte[]>("/health/fontes", { cache: "no-store" });
}
