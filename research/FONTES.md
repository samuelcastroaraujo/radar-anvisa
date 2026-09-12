# FONTES.md — Reconhecimento (M0)

Todas as requisições abaixo foram feitas de verdade nesta sessão, com
`User-Agent: RadarAnvisaBot-Research/0.1 (contato: mba5@nutropolis.com.br)`.
Amostras brutas (HTML/JSON tal como recebido) estão em `research/samples/`.
Nenhum endpoint abaixo foi assumido sem teste — o que não pôde ser testado
está marcado explicitamente como **PENDENTE**.

Data da coleta: 2026-09-12.

---

## 0. robots.txt — respeitado

### `anvisalegis.datalegis.net/robots.txt`
Arquivo: `samples/robots_anvisalegis.txt`.

- `User-agent: *` → `Content-Signal: search=yes,ai-train=no,use=reference` e `Allow: /`.
  Crawling genérico é **permitido** para um bot com UA próprio.
- Bots **nominalmente bloqueados** (`Disallow: /`): `Amazonbot`, `Applebot-Extended`,
  `Bytespider`, `CCBot`, **`ClaudeBot`**, `CloudflareBrowserRenderingCrawler`,
  `Google-Extended`, `GPTBot`, `meta-externalagent`.
  → **Decisão de projeto:** o ingestor nunca deve se identificar como `ClaudeBot`
  (nem qualquer bot da lista); usar um User-Agent próprio do projeto
  (`RadarAnvisaBot/1.0 (+contato)`), o que é permitido pela regra `User-agent: *`.
- Content-Signal declara `ai-train=no`: nunca usar o conteúdo coletado para
  treinar/ajustar modelos. `use=reference` é compatível com o uso pretendido
  (RAG com citação obrigatória da fonte, não geração livre).

### `www.gov.br/robots.txt`
Arquivo: `samples/robots_govbr.txt`.

- Restrições (`Disallow`) valem só para `/economia/pt-br/internet/*`, `/ebserh/*`,
  `/mre/*` e alguns padrões de views internas do Plone. **Nada em `/anvisa/`
  está bloqueado.**
- Sitemap: `https://www.gov.br/sitemap.xml.gz`.

**Regra de crawling adotada (para ambos os domínios):** UA identificado com
contato, máx. 1 req/s por domínio, backoff exponencial em 429/5xx, retry com jitter,
cache do bruto em `data/raw/{fonte}/{data}/`.

---

## 1. AnvisaLegis — https://anvisalegis.datalegis.net

Portal PHP, sessão via cookie `PHPSESSID` (criado automaticamente na primeira
requisição, não exige login para o conteúdo público de normas). Content-Type
declarado: **`text/html; charset=iso-8859-1`** — confirmado em todas as respostas
testadas.

### ⚠️ Encoding — ponto crítico validado

Existem **duas camadas de encoding diferentes** nesse portal, confirmadas com bytes crus:

1. **Páginas de navegação/listagem** (`categorias`, `abrirLegislacao`,
   `abrirResenhaAnoData`, etc.): acentos vêm como **bytes ISO-8859-1 reais**.
   Ex.: bytes `56 69 67 69 6C \xE2 6E 63 69 61` → decodificados com
   `.decode('iso-8859-1')` → `"Vigilância"` corretíssimo.
   Verificado byte a byte em `samples/abrirLegislacao_310.raw`
   (`Resolu\xe7\xf5es`, `Vigil\xe2ncia Sanit\xe1ria`).
2. **Página de texto integral do ato** (`abrirTextoAto`): o corpo usa
   **entidades HTML nomeadas** (`&Ccedil;`, `&Atilde;`, `&Aacute;`, `&ordm;`...)
   dentro de um documento que também é servido como `iso-8859-1`.
   Verificado em `samples/texto_rdc842_2024.raw`.

**Pipeline de decodificação obrigatório, nessa ordem:**
```python
text = response.content.decode("iso-8859-1")   # 1) decodifica os bytes
text = html.unescape(text)                       # 2) resolve entidades HTML
# normalizar para NFC e persistir sempre como UTF-8
```
Validado: após esse pipeline, `"Resolução" in text` e `"Vigilância Sanitária" in text`
retornam `True` nas amostras coletadas (a exibição de `�` em alguns `print()` deste
terminal Windows é só limitação do console — o dado em si, checado via `in`/`repr`
lido do arquivo `.html` salvo em UTF-8, está correto).

### Navegação — padrão de URL

`GET /action/ActionDatalegis.php?acao={ACAO}&cod_modulo={MOD}&cod_menu={MENU}[&ano=YYYY]`

Semente confirmada: `?acao=categorias&cod_modulo=310&menuOpen=true` → 200 OK,
lista os `cod_menu` reais de cada ação/módulo (não são inventados, foram extraídos
do HTML retornado).

### Módulo 310 — Normas regulatórias (P0)

| Ação | cod_menu | O que é | Testado |
|---|---|---|---|
| `abrirLegislacao` | 9434 | Página de busca de normas do módulo | ✅ 200, `samples/abrirLegislacao_310.raw` |
| `recuperarTematicasCollapse` | 9431 | Normas por tema | link confirmado, conteúdo não baixado ainda |
| `abrirResenhaAnoData` | 9882 | **Normas revogadas (2.438)** — confirma o número do enunciado, achado no texto: `NORMAS REVOGADAS (2.438)` | ✅ 200, `samples/revogadas_310.raw` |
| `abrirResenhaAnoData&ano=YYYY` | 9882 | Listagem de revogadas de um ano específico (parâmetro `ano` é real, extraído dos `href` da página anterior — testado com `ano=2024`) | ✅ 200, `samples/revogadas_310_ano2024.raw` |
| `abrirTextoAto` | — (link direto) | Texto integral de um ato específico | ✅ 200, `samples/texto_rdc842_2024.raw` |

Contagem de "vigentes" (1.138) e "alteradoras/outras" (464) **não foram
confirmadas numericamente ainda** — vistas apenas como rótulos de menu; a
contagem efetiva será validada no M2 ao varrer a listagem completa.

**Estrutura de item de lista** (`abrirResenhaAnoData&ano=2024`), por `<article class="ato">`:
```html
<article class="ato">
  <div class="contentInfo ...">
    <div class="ementa">
      <a href="/action/ActionDatalegis.php?acao=abrirTextoAto&link=S&tipo=RDC
                &numeroAto=00000842&seqAto=000&valorAno=2024
                &orgao=RDC/DC/ANVISA/MS&cod_modulo=310&cod_menu=9882"
         target="_blank">
        <span><i class="fas fa-chevron-down"></i>
          <span class="ico-situacao revogado">...</span>  <!-- status na própria classe CSS -->
        </span>
        ... "Revogada pela RESOLUÇÃO - RDC Nº 454, DE 17 DE DEZEMBRO DE 2020" ...
      </a>
    </div>
  </div>
</article>
```
→ `tipo`, `numeroAto`, `seqAto`, `valorAno`, `orgao` são os parâmetros estáveis
para identificar/buscar um ato específico. O status (`ico-situacao revogado`)
vem embutido na classe CSS do próprio item — outro sinal (além do texto livre)
para status_vigencia.

**Enumeração de `tipo_ato`** (extraída de um `<select>`/`value` real da página de
busca, não inventada): `DSN, ARO, ATA, ATO, AUT, COM, CPB, EXC, DSG, ETA, DEP,
EDT, ECO, EDL, PRG, MAN, ORT, PAR, APE, CNV, PTL, RLT, RES, RDC, REP, REN, DPS,
RHO, RAU, RAM, AUD, TMS, POR, IXL, ACO, VTO, REQ, NDT, DCS, AAP, BIB, ACM, EXL,
REC, TCP, DOA, MSA, CES, CMT, APO, CDS, CMS, DCP, DEM, DES, DIS, EQA, EXO, GDT,
TTB, CNC, NOM, TCM, PEN, POS, PRD, PRF, RQS, VAC, INM, INC, PCJ, PIM, PNT, AFA,
OSV, DLB, GRT, ETE, PLA, NTA, NTC, EMC, PAO, CON, ARR, EDC, CTD, CRI, TAP, DST,
EPP, DIN, GUI, AIR` (fonte: `samples/revogadas_310.raw`).

### Página de texto integral (`abrirTextoAto`) — estrutura confirmada

Amostra: `samples/texto_rdc842_2024.raw` (RDC 842/2024).

- Cabeçalho do ato como texto: `"RESOLUÇÃO - RDC Nº 842, DE 22 DE FEVEREIRO DE 2024"`
  seguido da ementa.
- **Aviso de revogação já pronto no HTML**, quando aplicável:
  ```html
  <em class="link-revogado">Revogada pela
    <a href="javascript:LinkTexto('RDC','00000955','000','2024',
                                    'RDC/DC/ANVISA/MS','','','')">
      Resolução da Diretoria Colegiada - RDC nº 955, de 20/12/2024
    </a>
  </em>
  ```
  → **não precisamos inferir por regex livre que um ato foi revogado**: quando
  existe, o próprio portal marca com `class="link-revogado"` e o link
  `LinkTexto(...)` já contém tipo/número/ano/órgão do ato revogador.
- **Referências cruzadas no corpo do texto** usam o mesmo padrão
  `javascript:LinkTexto(tipo, numeroAto, seqAto, valorAno, orgao, tipoDispositivo, numDispositivo, extra)`,
  ex.: `LinkTexto('LEI','00009782','000','1999','NI','A','15','')` referencia o
  "art. 15" da Lei 9.782/1999. O par `(tipoDispositivo='A', numDispositivo='15')`
  mapeia diretamente para o campo `norma_relacao.dispositivo` do schema
  (`art. 15`). **Isso é uma fonte estruturada de relações, muito mais confiável
  que regex sobre texto livre** — deve ser a fonte primária de
  `norma_relacao`, com o regex sobre `"Fica revogada..."/"Altera..."` como
  fallback apenas para o texto corrido fora dos links.

### Módulo 630 — Consultas Públicas (P1)

- `abrirLegislacao&cod_menu=9797`: página de entrada, lista subcategorias/anos
  via `abrirResenhaAno`, `abrirResenhaAnoAto`, `abrirResenhaAnoData` com
  **dezenas de `cod_menu` distintos** (um por área temática) — todos extraídos
  reais do HTML, não inventados. ✅ `samples/legislacao_630.raw`
- `abrirResenhaAno&cod_menu=9789`: listagem geral. ✅ 200,
  `samples/resenhaAno_630.raw`. **Não contém a palavra "prazo"** — o prazo de
  cada consulta pública provavelmente está só na página individual do ato
  (mesmo padrão `abrirTextoAto`). **Pendente**: abrir um item individual de CP
  aberta para confirmar onde/como o prazo aparece — necessário antes do M5.

### Push nativo (módulo 653) — plano B, não usar como dependência

Não testado neste M0 (não há amostra real). Registrado aqui só como referência
do enunciado: existe um módulo de notificação push nativo do portal
(`cod_modulo=653`), mas por instrução do projeto **não deve ser uma
dependência** do sistema de alertas — ficará como possível plano B a investigar
depois, se necessário.

---

## 2. Notícias e informes ANVISA — https://www.gov.br/anvisa/pt-br

CMS: **Plone + Volto** (frontend React), confirmado por `class="plone-..."` e
`window.__data` no HTML — não é mais o Plone clássico server-rendered puro, mas
o servidor ainda faz SSR do estado inicial (bom para nós: dá para raspar sem
Playwright).

### RSS — **não funciona** (contrariando a suposição do enunciado)

Testado com `curl` puro:

| URL testada | Resultado |
|---|---|
| `.../assuntos/noticias-anvisa/RSS` | **404** |
| `.../assuntos/alimentos/informes/RSS` | **404** |
| `.../assuntos/fiscalizacao-e-monitoramento/informes-de-seguranca/RSS` | **401** |

`Accept: application/json` no path normal ou em `++api++/...` também **não**
retorna JSON puro — a app sempre serve o HTML do Volto
(`content-type: text/html`, confirmado nos headers).

### Alternativa validada e funcionando: `window.__data` embutido no HTML

Toda página de listagem/artigo do gov.br/anvisa inclui um `<script>` com
`window.__data = {...}` (estado inicial do Redux do Volto) que contém, em
`content.data`, **exatamente o mesmo JSON que a API REST do Plone retornaria**:
`@id`, `@type`, `title`, `description`, `effective` (data de publicação),
`review_state`, `items`, `items_total`, `batching` (com `b_start` para
paginação), e para artigos individuais também `blocks` (corpo do texto,
dividido em blocos `html`/`title`/`description`/etc.).

**Padrão de extração validado:**
```python
start = html.find("window.__data=") + len("window.__data=")
end = html.find("</script>", start)
blob = html[start:end].rstrip().rstrip(";").replace(":undefined", ":null")
data = json.loads(blob)["content"]["data"]
```
(o `:undefined` → `:null` é necessário porque o Redux serializa
`location.state: undefined`, que não é JSON válido — confirmado, sem essa troca
o `json.loads` falha.)

Testado e funcionando em:
- `samples/noticias_anvisa_listagem.html` — pasta raiz de notícias, mostra
  subpastas por ano (`.../noticias-anvisa/2026`, `.../2025`, ...).
- `samples/noticias_2026_api.json` — pasta do ano 2026:
  `items_total: 572`, paginação `b_start=0,25,50,...` de 25 em 25,
  `@type: "News Item"` para as notícias reais (filtrar `Image`/`Document`
  fora), datas reais batendo com "hoje" (2026-09-12: já há itens de
  janeiro/2026 na amostra).
- `samples/noticia_exemplo.html` — artigo individual
  (`.../2026/anvisa-proibe-cosmeticos-e-saneantes-irregulares`): `blocks`
  contém o corpo em HTML, incluindo **links diretos para o DOU/in.gov.br** das
  resoluções mencionadas na notícia (ex.:
  `https://www.in.gov.br/web/dou/-/resolucao-re-n-19-de-5-de-janeiro-de-2026-679329463`)
  — útil para casar notícia ↔ norma automaticamente.

**Encoding desta fonte:** a resposta já vem em UTF-8 real
(`Content-Type: text/html; charset=utf-8` no header) — sem a armadilha do
AnvisaLegis. Confirmado.

**Decisão de arquitetura:** abandonar a suposição de RSS; o ingestor de
notícias faz `GET` da página HTML normal (listagem por ano + paginação
`?b_start=N`) e extrai `window.__data`, sem necessidade de Playwright.

### `assuntos/alimentos/informes` — confirmado, mesmo padrão

`GET` retorna 200, mesma estrutura Plone/Volto. `samples/informes_alimentos_listagem.html`.
Estrutura de itens ainda não explorada em profundidade (fica para M5).

### `assuntos/fiscalizacao-e-monitoramento/informes-de-seguranca` — ⚠️ ENDPOINT MUDOU

A URL do enunciado dá **404**. Path correto descoberto via navegação real
a partir de `.../fiscalizacao-e-monitoramento` (`samples/fiscalizacao_monitoramento.html`):
`.../fiscalizacao-e-monitoramento/informes-de-seguranca-1` (nota o `-1`).

Esse item é do tipo Plone `"Link"` (atalho) e, seguindo o redirect (`curl -L`),
**leva para um sistema totalmente diferente**:
```
https://consultas.anvisa.gov.br/#/alertas-sanitarios/q/?tipoAlerta=2&area=2,5
```
Isso é uma **SPA Angular com roteamento por hash** — não é mais gov.br/Plone.
`consultas.anvisa.gov.br/robots.txt` não existe (404, sem restrição declarada).
**Não investiguei a API REST que essa SPA consome** (precisa inspecionar as
chamadas XHR reais no navegador/DevTools, ou repo público se existir — nada
disso foi testado ainda).

**→ PARADA CONFORME INSTRUÇÃO: este endpoint específico (alertas de segurança)
mudou de plataforma. Alternativas para decisão:**
1. Investigar `consultas.anvisa.gov.br` como fonte própria (provável API JSON
   por trás da SPA) — exige nova sessão de recon dedicada antes de implementar.
2. Rebaixar esta fonte para P2/P3 (como o enunciado já sinaliza para módulo
   134) e cobrir "alertas de segurança" via DOU/INLABS + notícias, que já
   citam recolhimentos/proibições (visto no exemplo de notícia acima).
3. Ignorar por ora e revisitar no M5, documentando a lacuna no `/health`.

Recomendo (2) para não bloquear o M1/M2, com (1) como tarefa futura explícita.

---

## 3. DOU em tempo real — INLABS

Repositório oficial confirmado: `https://github.com/Imprensa-Nacional/inlabs`
(branch padrão é **`master`**, não `main`). Lido via API pública do GitHub
(sem autenticação), README e scripts baixados de verdade:
`samples/inlabs_readme.md`, `samples/inlabs-auto-download-xml.py`.

**Estrutura confirmada pelo script oficial** (`public/python/inlabs-auto-download-xml.py`)
**e testada de ponta a ponta nesta sessão, com conta real do usuário**
(credenciais em `.env`, nunca commitadas — `.env` está no `.gitignore`):

- Login: `POST https://inlabs.in.gov.br/logar.php`
  body `application/x-www-form-urlencoded`: `email=...&password=...`
  → resposta seta cookie `inlabs_session_cookie`. **Confirmado**: o campo
  precisa ser um e-mail de verdade (o form de cadastro em
  `inlabs.in.gov.br/acessar.php` tem `email`, `password`, `nome_completo`,
  `telefone`, `uf_cidade`, `nome_empresa` — é conta pessoal, não usuário
  arbitrário). Um `email` que não é um e-mail real falha silenciosamente
  (HTTP 200, mas sem `inlabs_session_cookie`, sem mensagem de erro visível).
- **Pegadinha real encontrada:** `p={YYYY-MM-DD}` só funciona se aquela pasta
  de fato existir no servidor. No momento do teste (2026-09-12), a pasta do
  dia corrente **ainda não existia** (o DOU do dia só fica disponível mais
  tarde) — pedir `p=2026-09-12` faz o servidor devolver **a página HTML do
  diretório raiz** (HTTP 200, ~39 KB) em vez de um erro, o que pode ser
  confundido com sucesso se só se checar o status code. **O ingestor real
  precisa validar `Content-Type: application/octet-stream` (ou o magic byte
  `PK` de ZIP) antes de aceitar o download, nunca só o HTTP 200.**
  Com uma data existente (`p=2026-09-11&dl=2026-09-11-DO1.zip`) o download
  funcionou: 3.959.655 bytes, `Content-Type: application/octet-stream`, zip
  válido com 512 arquivos.
- Header extra necessário: `origem: 736372697074` (hex de `"script"` em ASCII).
- Seções confirmadas na listagem real de um dia (`?p=2026-09-11`, sem `dl`):
  `DO1`, `DO1E`, `DO2`, `DO2E`, `DO3` (mais PDFs assinados por seção/anexo,
  não necessários para o pipeline de texto).

`inlabs.in.gov.br/robots.txt` → 404 (sem robots.txt publicado). Base `/` sem
sessão → **302** (redirect de login), confirmando que exige autenticação.

### Schema do XML — confirmado com arquivo real

Amostras em `samples/inlabs_extract/` (duas matérias reais da ANVISA, extraídas
do DO1 de 11/09/2026, publicadas com nomes truncados como `515_20260911_<id>.xml`).

```xml
<xml><article id="51230339" name="resolucao_3547_2026 GGFIS" idOficio="12049893"
  pubName="DO1" artType="Resolução" pubDate="11/09/2026"
  artClass="..." artCategory="Ministério da Saúde/Agência Nacional de
  Vigilância Sanitária/4ª Diretoria/Gerência-Geral de Inspeção e Fiscalização
  Sanitária" artSize="12" numberPage="175"
  pdfPage="http://pesquisa.in.gov.br/imprensa/jsp/visualiza/index.jsp?data=11/09/2026&amp;jornal=515&amp;pagina=175"
  editionNumber="172" idMateria="24349152">
  <body>
    <Identifica><![CDATA[ RESOLUÇÃO-RE nº 3.547, DE 9 DE SETEMBRO DE 2026]]></Identifica>
    <Data><![CDATA[]]></Data>
    <Ementa />
    <Texto><![CDATA[<p class="identifica">RESOLUÇÃO-RE nº 3.547...</p>...]]></Texto>
  </body>
  <Midias />
</article></xml>
```

**Encoding:** arquivo é **UTF-8 com BOM** (`\xef\xbb\xbf` no início) — bem mais
simples que o AnvisaLegis. Decodificar com `bytes.decode("utf-8-sig")`.
Validado: `"Resolução"`, `"Vigilância Sanitária"` e
`"Agência Nacional de Vigilância Sanitária"` saem corretos direto, sem
nenhuma camada extra de entidades HTML.

**Filtro confirmado por órgão:** filtrar por
`artCategory` contendo literalmente `"Agência Nacional de Vigilância
Sanitária"` (não usar um `"ANVISA"` genérico — no dia testado, 3 matérias de
outros órgãos citavam "ANVISA" no corpo do texto sem serem da ANVISA; filtrar
por substring solta em todo o XML gera falso positivo). No DO1 de 11/09/2026,
com esse filtro correto: **14 matérias da ANVISA** em 512 arquivos do dia
(tipos vistos: `Resolução` ×13, `Aresto` ×1).

`Identifica` dá o cabeçalho formatado do ato (regex simples extrai
tipo/número/ano dali), `Texto` traz o corpo em HTML (mesmas tags `<p>` do
AnvisaLegis, mas aqui sem entidades — já é texto pronto), e `pdfPage` é um
link público e estável para citar como fonte do DOU.

A API de consulta do in.gov.br **não foi testada**, por instrução explícita
(Cloudflare bot manager) — respeitado sem verificação adicional.

---

## 4. Dados abertos — https://www.gov.br/anvisa/pt-br/acessoainformacao/dadosabertos

Checagem rasa: `GET` 200, mesmo padrão Plone/Volto,
`samples/dados_abertos.html`. `items_total: 4`
(`arquivos`, `Dados Abertos`, `Dados abertos - capa` [Link], `Painéis e
Capacitações`). Baixo volume — catalogação completa fica para depois, filtrando
apenas o que for texto normativo/agenda regulatória, como pedido. Não é
bloqueante para M1/M2.

---

## Resumo do que ficou pendente (nada foi inventado além disto)

1. **`informes-de-seguranca`**: mudou para SPA em `consultas.anvisa.gov.br`;
   API real por trás não foi mapeada. Decisão pendente do usuário (ver seção 2).
2. **Módulo 630 (Consultas Públicas)**: falta abrir um item individual de CP
   aberta para confirmar onde o "prazo" aparece no HTML.
3. **Contagens 1.138 vigentes / 464 alteradoras / 291 revogadoras / 22
   retificadoras**: só a de revogadas (2.438) foi confirmada textualmente no
   menu; as demais serão validadas por contagem real no M2.
4. **Dados abertos**: catalogado só superficialmente (4 itens no nível
   raiz), sem descer nas subpastas ainda.

**Resolvido nesta rodada:** INLABS testado de ponta a ponta com conta real
(login, download, unzip, filtro por `artCategory`, encoding) — ver seção 3.

Nenhum endpoint usado no código (a partir do M1) deve ir além do que está
documentado e testado aqui. Qualquer ação nova (`acao=...`) encontrada durante
a implementação deve primeiro passar por uma requisição real de verificação,
com a amostra salva em `research/samples/`, antes de virar código de produção.
