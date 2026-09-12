import { Badge } from "@/components/ui/badge";

/**
 * `status_vigencia` é o campo mais crítico do produto (ver CLAUDE.md,
 * requisitos inegociáveis) — sempre visível perto do número da norma,
 * nunca só no texto corrido.
 */
export function NormaBadge({ status }: { status: string }) {
  if (status === "vigente") return <Badge variant="vigente">vigente</Badge>;
  if (status === "revogada") return <Badge variant="revogada">revogada</Badge>;
  return <Badge variant="outline">{status}</Badge>;
}
