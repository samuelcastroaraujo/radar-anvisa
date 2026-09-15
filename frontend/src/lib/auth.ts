/**
 * Login único e compartilhado (não é multiusuário — usuário/senha vêm de
 * variável de ambiente, nunca de banco). Roda tanto no middleware (Edge
 * runtime) quanto nas Route Handlers de login/logout (Node runtime) — por
 * isso usa só Web Crypto (`crypto.subtle`), disponível nos dois, em vez do
 * módulo `crypto` do Node (que não existe no Edge).
 *
 * O cookie de sessão não guarda usuário nem senha: é um HMAC-SHA256 de um
 * valor fixo, assinado com `AUTH_SECRET` — qualquer um que souber
 * `AUTH_SECRET` pode forjar a sessão, mas ninguém de fora tem esse segredo,
 * e não há necessidade de sessão por usuário (é um login só, compartilhado
 * pela equipe).
 */

const SESSION_COOKIE_NAME = "radar_auth";
const SESSION_PAYLOAD = "autenticado";

function segredo(): string {
  const valor = process.env.AUTH_SECRET;
  if (!valor) {
    // Sem AUTH_SECRET configurado, nenhuma sessão pode ser criada nem
    // validada — a porta fica fechada por padrão, nunca aberta por engano.
    throw new Error("AUTH_SECRET não configurado");
  }
  return valor;
}

async function chaveHmac(): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(segredo()),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
}

function paraHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/** Comparação em tempo aproximadamente constante — evita que a diferença
 * de tempo de um `===` vaze quantos caracteres bateram, mesmo sendo um
 * risco baixo pra uma ferramenta interna como esta. */
function compararSeguro(a: string, b: string): boolean {
  const tamanho = Math.max(a.length, b.length);
  let diferenca = a.length === b.length ? 0 : 1;
  for (let i = 0; i < tamanho; i++) {
    diferenca |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diferenca === 0;
}

export async function gerarTokenSessao(): Promise<string> {
  const chave = await chaveHmac();
  const assinatura = await crypto.subtle.sign("HMAC", chave, new TextEncoder().encode(SESSION_PAYLOAD));
  return paraHex(assinatura);
}

export async function tokenSessaoValido(token: string | undefined): Promise<boolean> {
  if (!token) return false;
  try {
    const esperado = await gerarTokenSessao();
    return compararSeguro(token, esperado);
  } catch {
    return false; // AUTH_SECRET ausente, por exemplo — nega, não derruba a página
  }
}

export function credenciaisValidas(usuario: string, senha: string): boolean {
  const usuarioEsperado = process.env.AUTH_USERNAME ?? "";
  const senhaEsperada = process.env.AUTH_PASSWORD ?? "";
  if (!usuarioEsperado || !senhaEsperada) {
    // Não configurado no ambiente -> nunca deixa passar (falha fechada).
    return false;
  }
  return compararSeguro(usuario, usuarioEsperado) && compararSeguro(senha, senhaEsperada);
}

export { SESSION_COOKIE_NAME };
