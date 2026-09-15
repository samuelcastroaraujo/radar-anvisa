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

export interface ProdutoCitado {
  numero_processo: string;
  descricao: string;
  situacao_registro: string | null;
  detentor_razao_social: string | null;
  url_origem: string;
}

export interface RespostaChat {
  resposta: string;
  fontes: string[];
  normas: NormaCitada[];
  produtos: ProdutoCitado[];
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

export interface FiltroTimeline {
  dias?: number;
  limite?: number;
  q?: string;
  dataInicio?: string; // YYYY-MM-DD
  dataFim?: string; // YYYY-MM-DD
}

export async function buscarTimeline(filtro: FiltroTimeline = {}): Promise<ItemTimeline[]> {
  const { dias = 30, limite = 100, q, dataInicio, dataFim } = filtro;
  const params = new URLSearchParams({ limite: String(limite) });
  // `data_inicio`/`data_fim` (quando presentes) mandam mais que `dias` no
  // backend — ver `app/main.py` — então só envia `dias` quando nenhuma
  // data explícita foi escolhida.
  if (dataInicio || dataFim) {
    if (dataInicio) params.set("data_inicio", dataInicio);
    if (dataFim) params.set("data_fim", dataFim);
  } else {
    params.set("dias", String(dias));
  }
  if (q && q.trim()) params.set("q", q.trim());
  return apiFetch<ItemTimeline[]>(`/timeline?${params.toString()}`, {
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

export interface ProdutoAlimento {
  numero_processo: string;
  numero_registro_ou_notificacao: string;
  descricao: string;
  situacao_registro: string | null;
  tipo_regularizacao: string | null;
  situacao_processo: string | null;
  detentor_cnpj: string | null;
  detentor_razao_social: string | null;
  categorias: string[];
  marcas: string[];
  data_regularizacao: string | null;
  data_atualizacao: string | null;
  mes_ano_vencimento: string | null;
  url_origem: string;
}

export interface BuscaProdutosAlimentos {
  itens: ProdutoAlimento[];
  pagina: number;
  tamanho_pagina: number;
}

export interface FiltroProdutosAlimentos {
  nomeProduto?: string;
  marca?: string;
  detentorRegistro?: string;
  numeroProcesso?: string;
  numeroRegistroNotificacao?: string;
  situacaoRegistro?: "Ativo" | "Inativo";
  pagina?: number;
  tamanhoPagina?: number;
}

/**
 * Consulta ao vivo (`GET /produtos/alimentos` no backend, que por sua vez
 * consulta a API real da ANVISA na hora — nada disso é indexado no banco,
 * ver `app/ingest/consultas_alimentos.py`). Sem `total`/`totalPages`
 * de propósito: a API da ANVISA devolve esses campos como função só do
 * `count` pedido, não do resultado real da busca (achado documentado em
 * `research/FONTES.md`, Addendum pós-M7) — por isso a paginação aqui é só
 * "próxima/anterior", nunca "página X de Y".
 */
export async function buscarProdutosAlimentos(
  filtro: FiltroProdutosAlimentos,
): Promise<BuscaProdutosAlimentos> {
  const {
    nomeProduto,
    marca,
    detentorRegistro,
    numeroProcesso,
    numeroRegistroNotificacao,
    situacaoRegistro,
    pagina = 1,
    tamanhoPagina = 10,
  } = filtro;
  const params = new URLSearchParams({
    pagina: String(pagina),
    tamanho_pagina: String(tamanhoPagina),
  });
  if (nomeProduto) params.set("nome_produto", nomeProduto);
  if (marca) params.set("marca", marca);
  if (detentorRegistro) params.set("detentor_registro", detentorRegistro);
  if (numeroProcesso) params.set("numero_processo", numeroProcesso);
  if (numeroRegistroNotificacao) {
    params.set("numero_registro_notificacao", numeroRegistroNotificacao);
  }
  if (situacaoRegistro) params.set("situacao_registro", situacaoRegistro);
  return apiFetch<BuscaProdutosAlimentos>(`/produtos/alimentos?${params.toString()}`, {
    cache: "no-store",
  });
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
