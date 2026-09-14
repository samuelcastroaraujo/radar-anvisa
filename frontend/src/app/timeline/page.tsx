import Link from "next/link";

import { buscarTimeline } from "@/lib/api";
import { formatarData } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Card, CardContent } from "@/components/ui/card";
import { TipoBadge } from "@/components/tipo-badge";
import { NormaBadge } from "@/components/norma-badge";

const JANELAS = [7, 30, 90, 365];
// Quando há busca por palavra-chave sem período explícito escolhido, vale
// mais mostrar o histórico inteiro do que travar em "últimos 30 dias" — o
// pedido original ("escrevo 243 e aparece as RDCs/INs desse número") não
// faz sentido só olhando o último mês.
const DIAS_MAXIMO = 3650;

function primeiro(valor: string | string[] | undefined): string | undefined {
  return Array.isArray(valor) ? valor[0] : valor;
}

export default async function TimelinePage({
  searchParams,
}: PageProps<"/timeline">) {
  const params = await searchParams;
  const diasParam = primeiro(params.dias);
  const q = primeiro(params.q) ?? "";
  const dataInicio = primeiro(params.data_inicio) ?? "";
  const dataFim = primeiro(params.data_fim) ?? "";
  const temPeriodoExplicito = Boolean(dataInicio || dataFim);

  let dias = Number(diasParam) > 0 ? Number(diasParam) : 30;
  if (q && !temPeriodoExplicito && !diasParam) {
    dias = DIAS_MAXIMO;
  }

  const itens = await buscarTimeline({
    dias,
    limite: 150,
    q: q || undefined,
    dataInicio: dataInicio || undefined,
    dataFim: dataFim || undefined,
  }).catch(() => null);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-semibold">Linha do tempo</h1>
        <div className="flex flex-wrap gap-1">
          {JANELAS.map((janela) => (
            <Link
              key={janela}
              href={`/timeline?dias=${janela}`}
              className={cn(
                "rounded-md border border-border px-2.5 py-1 text-xs hover:bg-accent",
                !temPeriodoExplicito && !q && dias === janela && "bg-accent font-medium",
              )}
            >
              {janela}d
            </Link>
          ))}
        </div>
      </div>

      <form
        action="/timeline"
        className="flex flex-wrap items-end gap-2 rounded-md border border-border p-3"
      >
        <div className="flex min-w-[200px] flex-1 flex-col gap-1">
          <label htmlFor="q" className="text-xs text-muted-foreground">
            Buscar (número, RDC, IN, palavra-chave...)
          </label>
          <input
            id="q"
            name="q"
            type="text"
            defaultValue={q}
            placeholder="ex.: 243"
            className="rounded-md border border-input bg-background px-2.5 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="data_inicio" className="text-xs text-muted-foreground">
            De
          </label>
          <input
            id="data_inicio"
            name="data_inicio"
            type="date"
            defaultValue={dataInicio}
            className="rounded-md border border-input bg-background px-2.5 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="data_fim" className="text-xs text-muted-foreground">
            Até
          </label>
          <input
            id="data_fim"
            name="data_fim"
            type="date"
            defaultValue={dataFim}
            className="rounded-md border border-input bg-background px-2.5 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <button
          type="submit"
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-90"
        >
          Filtrar
        </button>
        {(q || temPeriodoExplicito) && (
          <Link
            href="/timeline"
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent"
          >
            Limpar
          </Link>
        )}
      </form>

      {itens === null && (
        <p className="text-sm text-destructive">
          Não consegui carregar a linha do tempo — o backend está no ar?
        </p>
      )}

      {itens !== null && itens.length === 0 && (
        <p className="text-sm text-muted-foreground">
          {q
            ? `Nada encontrado para "${q}" no período selecionado.`
            : `Nada publicado no período selecionado.`}
        </p>
      )}

      <div className="space-y-2">
        {itens?.map((item, i) => (
          <Card key={`${item.tipo}-${item.titulo}-${i}`}>
            <CardContent className="flex items-start justify-between gap-3 py-3">
              <div className="min-w-0 space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <TipoBadge tipo={item.tipo} />
                  {item.status_vigencia && <NormaBadge status={item.status_vigencia} />}
                  <span className="text-xs text-muted-foreground">
                    {formatarData(item.data)}
                  </span>
                </div>
                {item.url ? (
                  <a
                    href={item.url}
                    target="_blank"
                    rel="noreferrer"
                    className="block truncate text-sm font-medium hover:underline"
                  >
                    {item.titulo}
                  </a>
                ) : (
                  <p className="truncate text-sm font-medium">{item.titulo}</p>
                )}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
