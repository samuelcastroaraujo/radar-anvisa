"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

const LINKS = [
  { href: "/", label: "Chat" },
  { href: "/timeline", label: "Linha do tempo" },
  { href: "/consultas-publicas", label: "Consultas públicas" },
  { href: "/produtos", label: "Registros de produtos" },
];

export function Nav() {
  const pathname = usePathname();

  // Tela de login não mostra os links do resto do site (ainda não
  // autenticado, e os links levariam de novo pro próprio middleware
  // redirecionar de volta pra cá).
  if (pathname === "/login") return null;

  return (
    <header className="border-b border-border">
      <div className="mx-auto flex max-w-4xl items-center gap-6 px-4 py-3">
        <span className="text-sm font-semibold tracking-tight">RADAR ANVISA</span>
        <nav className="flex flex-1 gap-1">
          {LINKS.map((link) => {
            const ativo = pathname === link.href;
            return (
              <Link
                key={link.href}
                href={link.href}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm transition-colors hover:bg-accent hover:text-accent-foreground",
                  ativo ? "bg-accent text-accent-foreground" : "text-muted-foreground",
                )}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>
        <form action="/api/logout" method="POST">
          <button
            type="submit"
            className="rounded-md px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
          >
            Sair
          </button>
        </form>
      </div>
    </header>
  );
}
