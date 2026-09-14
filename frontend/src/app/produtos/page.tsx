import Link from "next/link";

import { buscarProdutosAlimentos } from "@/lib/api";
import { formatarData } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { SituacaoRegistroBadge } from "@/components/situacao-registro-badge";

const TAMANHO_PAGINA = 10;

function primeiro(valor: string | string[] | undefined): string | undefined {
  return Array.isArray(valor) ? valor[0] : valor;
}

export default async function ProdutosPage({
  searchParams,
}: PageProps<"/produtos">) {
  const params = await searchParams;
  const nomeProduto = primeiro(params.nome_produto) ?? "";
  const marca = primeiro(params.marca) ?? "";
  const detentorRegistro = primeiro(params.detentor_registro) ?? "";
  const paginaParam = primeiro(params.pagina);
  const pagina = Number(paginaParam) > 0 ? Number(paginaParam) : 1;

  const temFiltro = Boolean(nomeProduto || marca || detentorRegistro);

  const resultado = temFiltro
    ? await buscarProdutosAlimentos({
        nomeProduto: nomeProduto || undefined,
        marca: marca || undefined,
        detentorRegistro: detentorRegistro || undefined,
        pagina,
        tamanhoPagina: TAMANHO_PAGINA,
      }).catch(() => null)
    : { itens: [], pagina: 1, tamanho_pagina: TAMANHO_PAGINA };

  // paginação "próxima/anterior", não "página X de Y" — a API da ANVISA
  // não devolve um total confiável (ver research/FONTES.md, Addendum
  // pós-M7), então uma página cheia é o único sinal que temos de que pode
  // haver mais itens.
  const podeTerProxima = (resultado?.itens.length ?? 0) === TAMANHO_PAGINA;

  function paginaHref(novaPagina: number): string {
    const p = new URLSearchParams();
    if (nomeProduto) p.set("nome_produto", nomeProduto);
    if (marca) p.set("marca", marca);
    if (detentorRegistro) p.set("detentor_registro", detentorRegistro);
    if (novaPagina > 1) p.set("pagina", String(novaPagina));
    const qs = p.toString();
    return qs ? `/produtos?${qs}` : "/produtos";
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-semibold">Registros de produtos (alimentos e suplementos)</h1>
      </div>
      <p className="text-sm text-muted-foreground">
        Consulta ao vivo direto na ANVISA (
        <a
          href="https://consultas.anvisa.gov.br/#/alimentos/"
          target="_blank"
          rel="noreferrer"
          className="underline hover:no-underline"
        >
          consultas.anvisa.gov.br
        </a>
        ) — não é uma base indexada aqui, cada busca consulta a fonte na hora.
      </p>

      <form
        action="/produtos"
        className="flex flex-wrap items-end gap-2 rounded-md border border-border p-3"
      >
        <div className="flex min-w-[200px] flex-1 flex-col gap-1">
          <label htmlFor="nome_produto" className="text-xs text-muted-foreground">
            Nome do produto
          </label>
          <input
            id="nome_produto"
            name="nome_produto"
            type="text"
            defaultValue={nomeProduto}
            placeholder="ex.: whey protein"
            className="rounded-md border border-input bg-background px-2.5 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div className="flex min-w-[160px] flex-1 flex-col gap-1">
          <label htmlFor="marca" className="text-xs text-muted-foreground">
            Marca
          </label>
          <input
            id="marca"
            name="marca"
            type="text"
            defaultValue={marca}
            placeholder="ex.: growth"
            className="rounded-md border border-input bg-background px-2.5 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div className="flex min-w-[180px] flex-1 flex-col gap-1">
          <label htmlFor="detentor_registro" className="text-xs text-muted-foreground">
            Empresa (razão social ou CNPJ)
          </label>
          <input
            id="detentor_registro"
            name="detentor_registro"
            type="text"
            defaultValue={detentorRegistro}
            placeholder="ex.: 12345678000199"
            className="rounded-md border border-input bg-background px-2.5 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <button
          type="submit"
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-90"
        >
          Buscar
        </button>
        {temFiltro && (
          <Link
            href="/produtos"
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent"
          >
            Limpar
          </Link>
        )}
      </form>

      {!temFiltro && (
        <p className="text-sm text-muted-foreground">
          Informe pelo menos um filtro (nome do produto, marca ou empresa) pra buscar.
        </p>
      )}

      {temFiltro && resultado === null && (
        <p className="text-sm text-destructive">
          Não consegui buscar na ANVISA agora — o backend está no ar?
        </p>
      )}

      {temFiltro && resultado !== null && resultado.itens.length === 0 && (
        <p className="text-sm text-muted-foreground">Nenhum produto encontrado.</p>
      )}

      <div className="space-y-2">
        {resultado?.itens.map((produto) => (
          <Card key={produto.numero_processo}>
            <CardContent className="space-y-2 py-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <a
                  href={produto.url_origem}
                  target="_blank"
                  rel="noreferrer"
                  className="text-sm font-medium hover:underline"
                >
                  {produto.descricao}
                </a>
                <div className="flex flex-wrap items-center gap-1">
                  <SituacaoRegistroBadge situacao={produto.situacao_registro} />
                  {produto.tipo_regularizacao && (
                    <Badge variant="secondary">{produto.tipo_regularizacao}</Badge>
                  )}
                </div>
              </div>
              <p className="text-sm text-muted-foreground">
                {produto.detentor_razao_social ?? "—"}
                {produto.detentor_cnpj ? ` · CNPJ ${produto.detentor_cnpj}` : ""}
              </p>
              {produto.categorias.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  {produto.categorias.join(", ")}
                </p>
              )}
              <p className="text-xs text-muted-foreground">
                Registro/notificação {produto.numero_registro_ou_notificacao} · Regularizado em{" "}
                {formatarData(produto.data_regularizacao)}
                {produto.mes_ano_vencimento && ` · Vencimento ${produto.mes_ano_vencimento}`}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>

      {temFiltro && resultado !== null && resultado.itens.length > 0 && (
        <div className="flex items-center justify-between gap-2 pt-1">
          {pagina > 1 ? (
            <Link
              href={paginaHref(pagina - 1)}
              className="rounded-md border border-border px-2.5 py-1 text-xs hover:bg-accent"
            >
              ← Anterior
            </Link>
          ) : (
            <span
              className={cn(
                "rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground opacity-50",
              )}
            >
              ← Anterior
            </span>
          )}
          <span className="text-xs text-muted-foreground">Página {pagina}</span>
          {podeTerProxima ? (
            <Link
              href={paginaHref(pagina + 1)}
              className="rounded-md border border-border px-2.5 py-1 text-xs hover:bg-accent"
            >
              Próxima →
            </Link>
          ) : (
            <span className="rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground opacity-50">
              Próxima →
            </span>
          )}
        </div>
      )}
    </div>
  );
}
