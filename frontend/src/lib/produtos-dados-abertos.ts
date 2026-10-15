/**
 * Busca de registro de produtos (alimentos/suplementos) direto nos dados
 * abertos da ANVISA — sem backend, sem banco.
 *
 * A consulta ao vivo de consultas.anvisa.gov.br passou a exigir Cloudflare
 * Turnstile; o CSV oficial abaixo não. Só roda no servidor do Next.js
 * (Server Components). Limitações do arquivo: não tem marca nem a
 * distinção notificado/registrado, e é um snapshot publicado pela ANVISA.
 */

import type { BuscaProdutosAlimentos, FiltroProdutosAlimentos, ProdutoAlimento } from "./api";

const URL_CSV = "https://dados.anvisa.gov.br/dados/DADOS_ABERTOS_ALIMENTO.csv";
const TTL_MS = 12 * 3600 * 1000;

interface Linha {
  cnpj: string;
  razaoSocial: string;
  produto: string;
  processo: string;
  categoria: string;
  dataFinalizacao: string | null;
  registro: string;
  vencimento: string | null;
  situacao: string;
  produtoN: string;
  razaoN: string;
}

let indice: Linha[] | null = null;
let indiceAte = 0;
let carregando: Promise<Linha[]> | null = null;

function norm(texto: string | undefined | null): string {
  return (texto ?? "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

const digitos = (t: string | undefined | null) => (t ?? "").replace(/\D/g, "");

/** "MM/DD/YYYY HH:MM:SS" -> ISO "YYYY-MM-DD" */
function dataIso(bruto: string | undefined): string | null {
  const m = /^(\d{2})\/(\d{2})\/(\d{4})/.exec((bruto ?? "").trim());
  return m ? `${m[3]}-${m[1]}-${m[2]}` : null;
}

function parseCsv(texto: string): Linha[] {
  const linhas = texto.split(/\r?\n/);
  const saida: Linha[] = [];
  for (let i = 1; i < linhas.length; i++) {
    if (!linhas[i]) continue;
    const c = linhas[i].split(";");
    if (c.length < 10) continue;
    const venc = dataIso(c[8]);
    saida.push({
      cnpj: digitos(c[0]),
      razaoSocial: c[1].trim(),
      produto: c[2].trim(),
      processo: digitos(c[3]),
      categoria: c[5].trim(),
      dataFinalizacao: dataIso(c[6]),
      registro: c[7].trim(),
      vencimento: venc ? `${venc.slice(5, 7)}/${venc.slice(0, 4)}` : null,
      situacao: c[9].trim(),
      produtoN: norm(c[2]),
      razaoN: norm(c[1]),
    });
  }
  return saida;
}

async function carregarIndice(): Promise<Linha[]> {
  if (indice && Date.now() < indiceAte) return indice;
  carregando ??= (async () => {
    try {
      const resp = await fetch(URL_CSV, { cache: "no-store" });
      if (!resp.ok) throw new Error(`dados abertos: HTTP ${resp.status}`);
      const buf = await resp.arrayBuffer();
      indice = parseCsv(new TextDecoder("windows-1252").decode(buf));
      indiceAte = Date.now() + TTL_MS;
      return indice;
    } catch (e) {
      if (indice) {
        indiceAte = Date.now() + 600_000; // snapshot velho > nada
        return indice;
      }
      throw e;
    } finally {
      carregando = null;
    }
  })();
  return carregando;
}

function paraProduto(l: Linha): ProdutoAlimento {
  return {
    numero_processo: l.processo,
    numero_registro_ou_notificacao: l.registro,
    descricao: l.produto,
    situacao_registro: l.situacao || null,
    tipo_regularizacao: null,
    situacao_processo: null,
    detentor_cnpj: l.cnpj || null,
    detentor_razao_social: l.razaoSocial || null,
    categorias: l.categoria ? [l.categoria] : [],
    marcas: [],
    data_regularizacao: l.dataFinalizacao,
    data_atualizacao: null,
    mes_ano_vencimento: l.vencimento,
    url_origem: `https://consultas.anvisa.gov.br/#/alimentos/${l.processo}/`,
    fonte: "dados_abertos",
  };
}

export async function buscarProdutosDadosAbertos(
  filtro: FiltroProdutosAlimentos,
): Promise<BuscaProdutosAlimentos> {
  const { pagina = 1, tamanhoPagina = 10 } = filtro;
  const nome = norm(filtro.nomeProduto);
  const marca = norm(filtro.marca);
  const detN = norm(filtro.detentorRegistro);
  const detDig = digitos(filtro.detentorRegistro);
  const ehCnpj =
    detDig.length >= 8 && detDig === (filtro.detentorRegistro ?? "").replace(/[.\-/\s]/g, "");
  const proc = digitos(filtro.numeroProcesso);
  const reg = (filtro.numeroRegistroNotificacao ?? "").trim();
  const detSemZeros = detDig.replace(/^0+/, "");

  const todas = await carregarIndice();
  const ini = (pagina - 1) * tamanhoPagina;
  const itens: ProdutoAlimento[] = [];
  let achados = 0;
  for (const l of todas) {
    if (filtro.situacaoRegistro && l.situacao !== filtro.situacaoRegistro) continue;
    if (nome && !l.produtoN.includes(nome)) continue;
    if (marca && !l.produtoN.includes(marca) && !l.razaoN.includes(marca)) continue;
    if (detN) {
      if (ehCnpj ? !l.cnpj.includes(detSemZeros) : !l.razaoN.includes(detN)) continue;
    }
    if (proc && l.processo !== proc) continue;
    if (reg && l.registro !== reg) continue;
    if (achados++ < ini) continue;
    itens.push(paraProduto(l));
    if (itens.length >= tamanhoPagina) break;
  }
  return { itens, pagina, tamanho_pagina: tamanhoPagina, fonte: "dados_abertos" };
}
