import Link from "next/link";

import { buscarConsultasPublicas } from "@/lib/api";
import { diasRestantes, formatarData } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";

export default async function ConsultasPublicasPage({
  searchParams,
}: PageProps<"/consultas-publicas">) {
  const params = await searchParams;
  const todasParam = Array.isArray(params.todas) ? params.todas[0] : params.todas;
  const apenasAbertas = todasParam !== "1";

  const cps = await buscarConsultasPublicas(apenasAbertas).catch(() => null);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Consultas públicas</h1>
        <div className="flex gap-1">
          <Link
            href="/consultas-publicas"
            className={cn(
              "rounded-md border border-border px-2.5 py-1 text-xs hover:bg-accent",
              apenasAbertas && "bg-accent font-medium",
            )}
          >
            Abertas
          </Link>
          <Link
            href="/consultas-publicas?todas=1"
            className={cn(
              "rounded-md border border-border px-2.5 py-1 text-xs hover:bg-accent",
              !apenasAbertas && "bg-accent font-medium",
            )}
          >
            Todas
          </Link>
        </div>
      </div>

      {cps === null && (
        <p className="text-sm text-destructive">
          Não consegui carregar as consultas públicas — o backend está no ar?
        </p>
      )}

      {cps !== null && cps.length === 0 && (
        <p className="text-sm text-muted-foreground">
          Nenhuma consulta pública {apenasAbertas ? "aberta" : "encontrada"} no momento.
        </p>
      )}

      <div className="space-y-2">
        {cps?.map((cp) => {
          const restantes = diasRestantes(cp.prazo_fim);
          return (
            <Card key={cp.url}>
              <CardContent className="space-y-2 py-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <a
                    href={cp.url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-sm font-medium hover:underline"
                  >
                    {cp.titulo}
                  </a>
                  {cp.aberta ? (
                    <Badge variant="vigente">
                      {restantes !== null && restantes >= 0
                        ? `${restantes} dia${restantes === 1 ? "" : "s"} restantes`
                        : "aberta"}
                    </Badge>
                  ) : (
                    <Badge variant="outline">encerrada</Badge>
                  )}
                </div>
                {cp.assunto && (
                  <p className="text-sm text-muted-foreground">{cp.assunto}</p>
                )}
                <p className="text-xs text-muted-foreground">
                  DOU: {formatarData(cp.data_dou)} · Prazo:{" "}
                  {formatarData(cp.prazo_inicio)} a {formatarData(cp.prazo_fim)}
                </p>
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
