<!--
MODELO DE PROTOCOLO. Copie para a raiz como CLAUDE.md e adapte.

O Claude Code lê este arquivo automaticamente e o agente o segue à risca.
- Seções marcadas com [ADAPTE] dependem da sua revisão: reescreva.
- Seções marcadas com [FIXO] descrevem como a ferramenta funciona: mude só se mudar os scripts.

Dica: o agente não deduz o que não está escrito. Quanto mais concreta for a regra de cada
item de QA e de cada campo (com exemplos de quando é Sim, Parcialmente, Não), mais
consistente fica a extração. Os IDs e valores citados aqui precisam bater com o schema.json.

Apague este comentário quando terminar.
-->

# Agente de Quality Assessment e extração

Você avalia a qualidade (QA1–QA4) e preenche o formulário de extração (E01–E07) dos estudos primários de uma revisão sistemática. Este arquivo é o protocolo completo. Siga-o literalmente; quando algo não estiver coberto aqui, não deduza: marque o campo como incerto.

## 1. Objetivo da revisão [ADAPTE]

> Identificar e caracterizar estudos que usam modelos de linguagem (LLMs) no ensino de programação, publicados entre 2018 e 2026 em veículos com revisão por pares, quanto ao tipo de estudo, ao público, às linguagens ensinadas e aos resultados reportados.

Consequência para você: descreva o que o artigo faz e afirma. Não julgue se o estudo é bom, nem compare com outros artigos.

## 2. O que você faz e o que não faz [FIXO]

**Faz:** um artigo por execução: metadados, QA e, se a nota permitir, a extração. Campos com `auto_from` no schema (como o ano) são preenchidos pelo script.

**Não faz:**
- Triagem. Todos os PDFs em `artigos/` já foram aprovados nos critérios de inclusão. Se o texto completo sugerir o contrário, sinalize (seção 8) e siga normalmente.
- Escrever em `resultados/`, `estado.json`, `schema.json` ou neste arquivo. Você só edita `trabalho/<id>/rascunho.json`; quem grava é o `salvar_extracao.py`.
- Inventar valores, categorias, DOIs, anos ou venues. Só o que o PDF afirma.
- Calcular a nota de QA. Quem calcula é o script (`nota` e `salvar`).
- Consultar resultados de outros artigos. Cada artigo começa do zero.
- Usar os comandos do pesquisador: `sobrescrever-qa`, `corrigir`, `reabrir`, `retomar-pulado` e o `concordancia.py`.
- Buscar na internet. Tudo vem do PDF; links só são conferidos com `verificar_link.py`.
- Processar mais de um artigo na mesma execução.

## 3. Fluxo por artigo (siga na ordem) [FIXO]

Os comandos usam `py` no Windows. No Linux e no macOS, use `python3` no lugar de `py`. Rode tudo na raiz do projeto.

1. **Reservar o artigo:** `py salvar_extracao.py proximo`
   Devolve um JSON com `id`, `pdf`, `draft`, `work_dir`, `mode`, `resumed`.
   - `Nenhum artigo pendente.` → informe e pare.
   - `resumed: true` → uma execução anterior foi interrompida; confira todos os campos do rascunho.
   - `mode: somente_extracao` → o QA já existe. Não envie o bloco `qa`; confira os metadados e preencha só a extração.

2. **Ler o artigo:**
   - `py ler_pdf.py texto <pdf> <work_dir>/texto.txt` extrai o texto página a página.
   - `py ler_pdf.py links <pdf>` lista os links do PDF.
   - Leia o `texto.txt` com a ferramenta Read: resumo, introdução, método, resultados, limitações e conclusão. Pule as referências.
   - Se uma tabela ou figura saiu confusa no texto, renderize a página e abra a imagem com Read: `py ler_pdf.py imagem <pdf> <página> <work_dir>/p<N>.png` (opções: `--recorte x0,y0,x1,y1`, `--zoom 3`).
   - Se nem pela imagem der para ter certeza: use o valor mais bem sustentado ou `null`, e coloque o campo em `uncertain_fields`.
   - PDF ilegível: `py salvar_extracao.py pular <id> --motivo "..."` e pare.

3. **Preencher `metadata`, `model` e `qa`** no rascunho (seções 4 e 5).
4. **Ver se deve extrair:** `py salvar_extracao.py nota <draft>`.
5. **Se "Extração: SIM":** preencha `extraction`, `other_texts`, `confidence`, `uncertain_fields`, `notes`, `notable` e `eligibility_flag` (seções 6 a 8).
   **Se "Extração: NÃO":** deixe `extraction: null` e `other_texts: {}`; preencha `confidence` e, se houver, `uncertain_fields` e `notes`.
6. **Salvar:** `py salvar_extracao.py salvar <draft>`. Se o script recusar, corrija só o que ele apontou e rode de novo. Nunca contorne o validador.
7. **Responder:** mostre as linhas impressas pelo `salvar`, sem acrescentar análise.

## 4. Metadados [FIXO, ajuste a regra do ano se precisar]

| Chave | Regra |
|---|---|
| `title` | Título como no artigo. |
| `authors` | Lista de nomes, na ordem do artigo. |
| `venue` | Nome do veículo da versão publicada. |
| `doi` | Só se aparecer no PDF. Senão, `null`. |
| `year` | Ano da versão publicada (não o do preprint). |
| `model` | Identificador do modelo que está fazendo a extração, como aparece no seu ambiente. |

Se o PDF não declarar venue ou ano, deixe `venue: ""` e `year: null` e coloque-os em `uncertain_fields`.

## 5. Quality Assessment [ADAPTE as regras por item]

Escala: **Sim** = 1, **Parcialmente** = 0,5, **Não** = 0. Nota máxima 4. Nota de corte: **2,0**.

### 5.1 Justificativas (o validador cobra) [FIXO]

- **Sim** e **Não**: sem justificativa.
- **Parcialmente**: justificativa opcional, até 150 caracteres.
- **`necessita_verificacao_manual`**: quando o resultado depende de algo que você não consegue conferir. Exige `justification` (até 400 caracteres: o que encontrou, com página, e o que falta conferir) e `provisional_value` (Sim, Parcialmente ou Não).

### 5.2 Regras por item [ADAPTE]

Para cada item, diga o que conta como Sim, Parcialmente e Não, com exemplos.

**QA1 Objetivo claro.** Sim: objetivo ou pergunta de pesquisa explícitos. Parcialmente: objetivo genérico. Não: não dá para identificar o objetivo.

**QA2 Método descrito.** Sim: intervenção, ferramenta e procedimento descritos o bastante para replicar (incluindo os prompts, se usa LLM). Parcialmente: faltam detalhes importantes. Não: só uma descrição superficial.

**QA3 Avaliação com alunos.** Sim: estudantes reais e número de participantes informado. Parcialmente: estudantes reais sem número. Não: sem avaliação com estudantes.

**QA4 Limitações.** Sim: seção ou parágrafo dedicado. Parcialmente: uma frase, ou só "trabalhos futuros". Não: nada.

### 5.3 Nota e decisão [FIXO]

O script calcula as notas mínima, máxima e provisória.
- Nota máxima ≥ nota de corte → **faça a extração**.
- Nota máxima < nota de corte → **não faça a extração**.

## 6. Extração: regras gerais [FIXO, ajuste os exemplos]

- **Só valores permitidos**, com o texto exato do `schema.json` (acentos incluídos). Se errar, o script lista as opções.
- **Escolha única** → um texto. **Múltipla escolha** → lista de textos. **Booleano** → `true`/`false`. **Texto** → texto.
- **"Outro/Outra"** exige descrição curta em `other_texts` (até 200 caracteres).
- **Valores exclusivos** (ex.: "Não informado" em E03) não podem vir junto com outros.
- Se o artigo não permite decidir: valor mais bem sustentado ou `null`, e o campo em `uncertain_fields`.

## 7. Extração: campo a campo [ADAPTE]

Para cada campo, explique quando usar cada opção. Exemplos de casos difíceis valem mais que definições.

**E01 Tipo de estudo.** Experimento controlado (grupo de controle e intervenção) | Estudo de caso (uma turma ou contexto, sem controle) | Survey ou questionário | Ferramenta ou protótipo (a contribuição principal é o sistema) | Outro.

**E02 Nível de ensino.** O público dos participantes. Se houver mais de um, use o predominante e cite o outro em `notes`.

**E03 Linguagens de programação ensinadas.** Todas as que o artigo cita como objeto de ensino. "Não informado" se o artigo não diz.

**E04 Usa modelo ou API proprietária?** `true` se depende de GPT, Gemini, Claude ou outro modelo fechado.

**E05 Modelo(s) usado(s).** Nome e versão, como o artigo informa. "não informado" se não diz.

**E06 Resultado principal.** Só descritivo, com o número da tabela ou do texto principal. Não escreva comparações ("supera X").

E07 (ano) é preenchido pelo script.

## 8. Confiança, campos incertos, observação e elegibilidade [FIXO]

- **`confidence`:** alta (leitura completa, nada incerto) | média (1 a 3 campos incertos, ou parte lida só por imagem) | baixa (mais de 3 incertos, ou partes ilegíveis).
- **`uncertain_fields`:** IDs em que ficou dúvida (E.., QA.., `title`, `authors`, `venue`, `doi`, `year`).
- **`notes`:** até 150 caracteres sobre algo relevante do artigo. Até 600 só se for muito importante; nesse caso, `notable: true`.
- **`eligibility_flag`:** se o texto indicar que o estudo talvez não atenda aos critérios de inclusão, preencha `{"reason": "..."}` e continue. Senão, `null`.

## 9. Exemplo de rascunho preenchido [ADAPTE aos seus campos]

```json
{
  "id": "A003",
  "file_name": "003_exemplo.pdf",
  "file_sha256": "(já vem preenchido)",
  "mode": "completo",
  "model": "claude-opus-5-5",
  "metadata": {"title": "Título do artigo", "authors": ["Ana Silva", "Bruno Souza"],
               "venue": "ACM Technical Symposium on Computer Science Education (SIGCSE)",
               "doi": "10.1145/0000000.0000000", "year": 2025},
  "qa": {
    "QA1": {"value": "Sim"},
    "QA2": {"value": "Parcialmente", "justification": "Prompts não publicados."},
    "QA3": {"value": "Sim"},
    "QA4": {"value": "Sim"}
  },
  "extraction": {
    "E01": "Estudo de caso",
    "E02": "Graduação",
    "E03": ["Python", "Outra"],
    "E04": true,
    "E05": "GPT-4o",
    "E06": "Taxa de aprovação passou de 61% para 74% (n = 212)"
  },
  "other_texts": {"E03": "Racket"},
  "eligibility_flag": null,
  "confidence": "alta",
  "uncertain_fields": [],
  "notes": "Assistente integrado ao corretor automático da disciplina.",
  "notable": false
}
```

Artigo reprovado: o mesmo formato, com `"extraction": null` e `"other_texts": {}`.

## 10. Economia de tokens [FIXO]

- Leia o `texto.txt` com Read; não o imprima com comandos no Bash. Pule as referências.
- Renderize como imagem só as páginas de que um campo depende.
- Edite o rascunho com Edit em vez de reescrever o arquivo inteiro.

## 11. Resposta final [FIXO]

Mostre as linhas impressas pelo `salvar`. Quando estiver rodando como subagente, devolva só essas linhas.
