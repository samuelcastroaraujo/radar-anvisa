import Link from "next/link";

import { buscarTimeline } from "@/lib/api";
import { formatarData } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Card, CardContent } from "@/components/ui/card";
import { TipoBadge } from "@/components/tipo-badge";
import { NormaBadge } from "@/components/norma-badge";

const JANELAS = [7, 30, 90, 365];

export default async function TimelinePage({
  searchParams,
}: PageProps<"/timeline">) {
  const params = await searchParams;
  const diasParam = Array.isArray(params.dias) ? params.dias[0] : params.dias;
  const dias = Number(diasParam) > 0 ? Number(diasParam) : 30;

  const itens = await buscarTimeline(dias, 150).catch(() => null);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Linha do tempo</h1>
        <div className="flex gap-1">
          {JANELAS.map((janela) => (
            <Link
              key={janela}
              href={`/timeline?dias=${janela}`}
              className={cn(
                "rounded-md border border-border px-2.5 py-1 text-xs hover:bg-accent",
                dias === janela && "bg-accent font-medium",
              )}
            >
              {janela}d
            </Link>
          ))}
        </div>
      </div>

      {itens === null && (
        <p className="text-sm text-destructive">
          Não consegui carregar a linha do tempo — o backend está no ar?
        </p>
      )}

      {itens !== null && itens.length === 0 && (
        <p className="text-sm text-muted-foreground">
          Nada publicado nos últimos {dias} dias.
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
