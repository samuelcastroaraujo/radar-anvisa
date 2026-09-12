import { Badge } from "@/components/ui/badge";

const ROTULOS: Record<string, string> = {
  norma: "Norma",
  noticia: "Notícia",
  consulta_publica: "Consulta pública",
  dou: "DOU",
};

export function TipoBadge({ tipo }: { tipo: string }) {
  return <Badge variant="secondary">{ROTULOS[tipo] ?? tipo}</Badge>;
}
