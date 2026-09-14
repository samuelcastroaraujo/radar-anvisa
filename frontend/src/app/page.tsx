"use client";

import { useState, useRef, useEffect, type FormEvent } from "react";
import { SendHorizontal, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { NormaBadge } from "@/components/norma-badge";
import type { NormaCitada } from "@/lib/api";

interface Mensagem {
  id: string;
  papel: "usuario" | "assistente" | "erro";
  texto: string;
  fontes?: string[];
  normas?: NormaCitada[];
}

export default function ChatPage() {
  const [mensagens, setMensagens] = useState<Mensagem[]>([]);
  const [pergunta, setPergunta] = useState("");
  const [carregando, setCarregando] = useState(false);
  const fimRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fimRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [mensagens]);

  async function enviar(texto: string) {
    const pergunta = texto.trim();
    if (!pergunta || carregando) return;

    setMensagens((atual) => [
      ...atual,
      { id: crypto.randomUUID(), papel: "usuario", texto: pergunta },
    ]);
    setPergunta("");
    setCarregando(true);

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mensagem: pergunta }),
      });
      const dados = await resp.json();
      if (!resp.ok) {
        throw new Error(dados.detail ?? "erro desconhecido");
      }
      setMensagens((atual) => [
        ...atual,
        {
          id: crypto.randomUUID(),
          papel: "assistente",
          texto: dados.resposta,
          fontes: dados.fontes,
          normas: dados.normas,
        },
      ]);
    } catch (erro) {
      setMensagens((atual) => [
        ...atual,
        {
          id: crypto.randomUUID(),
          papel: "erro",
          texto:
            erro instanceof Error
              ? `Não consegui responder: ${erro.message}`
              : "Não consegui responder.",
        },
      ]);
    } finally {
      setCarregando(false);
    }
  }

  function aoSubmeter(evento: FormEvent) {
    evento.preventDefault();
    void enviar(pergunta);
  }

  return (
    <div className="flex h-[calc(100vh-8rem)] flex-col gap-4">
      <div className="flex-1 space-y-4 overflow-y-auto pr-1">
        {mensagens.length === 0 && (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-center text-muted-foreground">
            <p className="text-sm">
              Pergunte sobre normas, notícias ou consultas públicas da ANVISA.
            </p>
          </div>
        )}

        {mensagens.map((msg) => (
          <MensagemBolha key={msg.id} mensagem={msg} />
        ))}

        {carregando && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Consultando a base regulatória...
          </div>
        )}
        <div ref={fimRef} />
      </div>

      <form onSubmit={aoSubmeter} className="flex gap-2">
        <Input
          value={pergunta}
          onChange={(e) => setPergunta(e.target.value)}
          placeholder="Digite sua pergunta..."
          disabled={carregando}
        />
        <Button type="submit" disabled={carregando || !pergunta.trim()} size="icon">
          <SendHorizontal className="size-4" />
        </Button>
      </form>
    </div>
  );
}

function MensagemBolha({ mensagem }: { mensagem: Mensagem }) {
  if (mensagem.papel === "usuario") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground">
          {mensagem.texto}
        </div>
      </div>
    );
  }

  if (mensagem.papel === "erro") {
    return (
      <div className="max-w-[80%] rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
        {mensagem.texto}
      </div>
    );
  }

  return (
    <Card className="max-w-[85%]">
      <CardContent className="space-y-3 pt-4">
        <p className="whitespace-pre-wrap text-sm">{mensagem.texto}</p>

        {!!mensagem.normas?.length && (
          <div className="flex flex-wrap gap-2 border-t border-border pt-3">
            {mensagem.normas.map((norma) => (
              <a
                key={norma.id}
                href={norma.url_origem}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs hover:bg-accent"
              >
                <span className="font-medium">
                  {norma.tipo_ato} {norma.numero}/{norma.ano}
                </span>
                <NormaBadge status={norma.status_vigencia} />
              </a>
            ))}
          </div>
        )}

        {!!mensagem.fontes?.length && (
          <div className="space-y-1 border-t border-border pt-3 text-xs text-muted-foreground">
            <p className="font-medium">Fontes</p>
            <ul className="list-inside list-disc">
              {mensagem.fontes.map((fonte) => (
                <li key={fonte} className="truncate">
                  {fonte}
                </li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
