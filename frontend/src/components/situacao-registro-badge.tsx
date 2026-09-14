import { Badge } from "@/components/ui/badge";

/**
 * Situação do registro/notificação do produto (Ativo/Inativo) — reaproveita
 * as cores de `NormaBadge` (vigente=verde/revogada=vermelho): mesma lógica
 * de "isso é a informação mais crítica, tem que estar sempre visível como
 * cor, não só como texto corrido".
 */
export function SituacaoRegistroBadge({ situacao }: { situacao: string | null }) {
  if (situacao === "Ativo") return <Badge variant="vigente">Ativo</Badge>;
  if (situacao === "Inativo") return <Badge variant="revogada">Inativo</Badge>;
  return <Badge variant="outline">{situacao ?? "desconhecido"}</Badge>;
}
