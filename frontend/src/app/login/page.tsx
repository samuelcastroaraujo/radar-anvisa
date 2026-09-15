function primeiro(valor: string | string[] | undefined): string {
  return (Array.isArray(valor) ? valor[0] : valor) ?? "";
}

export default async function LoginPage({ searchParams }: PageProps<"/login">) {
  const params = await searchParams;
  const erro = primeiro(params.erro) === "1";
  const next = primeiro(params.next);

  return (
    <div className="mx-auto flex w-full max-w-sm flex-col gap-4 pt-16">
      <div>
        <h1 className="text-lg font-semibold">RADAR ANVISA</h1>
        <p className="text-sm text-muted-foreground">Acesso restrito — entre com login e senha.</p>
      </div>

      {erro && (
        <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          Usuário ou senha incorretos.
        </p>
      )}

      <form method="POST" action="/api/login" className="flex flex-col gap-3">
        <input type="hidden" name="next" value={next} />
        <div className="flex flex-col gap-1">
          <label htmlFor="usuario" className="text-xs text-muted-foreground">
            Usuário
          </label>
          <input
            id="usuario"
            name="usuario"
            type="text"
            required
            autoFocus
            autoComplete="username"
            className="rounded-md border border-input bg-background px-2.5 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="senha" className="text-xs text-muted-foreground">
            Senha
          </label>
          <input
            id="senha"
            name="senha"
            type="password"
            required
            autoComplete="current-password"
            className="rounded-md border border-input bg-background px-2.5 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <button
          type="submit"
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-90"
        >
          Entrar
        </button>
      </form>
    </div>
  );
}
