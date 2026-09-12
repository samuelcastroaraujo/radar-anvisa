export function formatarData(iso: string | null): string {
  if (!iso) return "—";
  const data = new Date(iso);
  if (Number.isNaN(data.getTime())) return "—";
  return data.toLocaleDateString("pt-BR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
}

export function diasRestantes(prazoFimIso: string | null): number | null {
  if (!prazoFimIso) return null;
  const fim = new Date(`${prazoFimIso}T00:00:00`);
  if (Number.isNaN(fim.getTime())) return null;
  const hoje = new Date();
  hoje.setHours(0, 0, 0, 0);
  return Math.round((fim.getTime() - hoje.getTime()) / (1000 * 60 * 60 * 24));
}
