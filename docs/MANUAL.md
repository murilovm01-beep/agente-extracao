# Agente de Quality Assessment e extração — configurar e rodar

O agente lê um artigo por vez, avalia QA1–QA7 e, se a nota máxima possível for ≥ 3,5, preenche E01–E20. Ele só edita um rascunho em `trabalho/<id>/`; quem valida e grava em `resultados/` é o `salvar_extracao.py`. O `estado.json` é recalculado a partir dos arquivos a cada comando, então dá para parar e retomar a qualquer momento sem perder nada.

## O que tem na pasta

| Arquivo | Para quê |
|---|---|
| `CLAUDE.md` | Protocolo que o agente segue (regras de QA e de cada campo). |
| `schema.json` | Valores permitidos de cada campo (texto exato do Parsif.al) e regras. |
| `salvar_extracao.py` | Valida, grava, controla o estado; comandos de verificação manual. |
| `ler_pdf.py` | Extrai texto, links e imagens de páginas do PDF. |
| `verificar_link.py` | Confere links de código e dados (QA5 e QA6). |
| `estatisticas.py` | Gráficos (PNG e PDF), tabelas CSV e relatório HTML. |
| `concordancia.py` | Amostra cega e concordância agente × você. |
| `estado.json` | Estado e contadores, criado na primeira execução (não edite). |
| `.claude/` | Permissões do projeto, subagente `extrator` e comandos `/proximo` e `/lote`. |
| `modelos/` | Modelos comentados de `CLAUDE.md` e `schema.json` para adaptar a outra revisão. |
| `artigos/` | Coloque aqui os PDFs aprovados na triagem. |
| `resultados/` | Um JSON por artigo (registro oficial). `historico/` guarda versões anteriores. |
| `relatorios/`, `conferencia/`, `trabalho/` | Saídas, conferência humana e rascunhos. |

## 1. Preparar (uma vez)

> Os comandos deste manual usam `py`, do Windows. No Linux e no macOS, troque por `python3`.

1. **Python:** `py -m pip install -r requirements.txt`
2. **Claude Code e login:** veja a seção *Login* do [README](../README.md) (conta do plano Claude ou chave de API com créditos).
3. **PDFs:** copie para `artigos/`. A ordem de processamento é a ordem alfabética dos nomes, então use prefixo numérico (`001_...pdf`, `002_...pdf`). Renomear depois não tem problema (o id segue o conteúdo do arquivo); cópias idênticas são ignoradas.
4. **Primeira abertura:** ao rodar `claude` na pasta, aceite a pergunta de confiança na pasta (*trust*). Sem isso as permissões do projeto não valem e ele vai pedir autorização a cada comando.
5. **Modelo:** escolha com `/model` e use o mesmo do início ao fim. O identificador fica gravado em cada resultado e vai no artigo.

## 2. Rodar

**Recomendado — em lote:** `/lote 10`
Cada artigo roda num subagente com contexto limpo; a sessão principal só coordena. Repita até aparecer "Nenhum artigo pendente". Comece com `/lote 3` e confira os resultados antes de soltar lotes maiores.

**Alternativa — um por vez:** `/proximo`, e depois `/clear` antes do próximo.

A cada artigo aparecem linhas assim:

```
[A012] 012_lasp.pdf — QA 4,5 provisória, faixa 3,5–5,5/7 (aprovado) — extração: sim
Verificar manualmente: QA5, QA6
Sinalizações: campos_incertos: E10 | confianca_baixa: confiança baixa
Progresso: 12/80 concluídos (aprovados 9, reprovados 2, dependem de verificação 1; sinalizados 5) — faltam 68
```

- Parou no meio (limite do plano, janela fechada, queda)? Rode de novo: o artigo em andamento é retomado com o rascunho mantido.
- Progresso a qualquer momento, em qualquer terminal: `py salvar_extracao.py status`.
- Se o Claude Code pedir permissão para rodar `py ...`, escolha "Yes, and don't ask again". Edição em `resultados/`, `estado.json`, `schema.json`, `CLAUDE.md` e nos scripts é bloqueada para o agente, assim como internet e os comandos de verificação manual.

## 3. Depois de processar tudo

Faça nesta ordem — a conferência cega vem antes de você abrir os resultados.

**3.1 Conferência cega (amostra de 15%, mínimo 10)**
1. `py concordancia.py amostra` — sorteia (semente 42, estratificada entre aprovados e reprovados) e cria um formulário por artigo em `conferencia/formularios/`. Ele lista os artigos em `conferencia/amostra.csv`.
2. Abra cada formulário no Excel e preencha a coluna `valor` lendo só o PDF. Múltipla escolha: separe com `|`. Preencha a extração mesmo nos artigos que você reprovaria. Salve como CSV.
3. `py concordancia.py comparar` — gera `resumo_concordancia.txt` (κ de Cohen nos itens de QA, na decisão de corte e nos campos de escolha única; Jaccard nos de múltipla escolha), `concordancia_por_campo.csv` e `divergencias.csv`.

**3.2 Verificações manuais**
1. `py salvar_extracao.py exportar` — gera em `relatorios/`: `consolidado.csv/json`, `qa_para_verificar.csv`, `revisao.csv` e `outros.csv` (textos de "Outro").
2. Para cada linha de `qa_para_verificar.csv`, confira o link e registre:
   `py salvar_extracao.py sobrescrever-qa A012 QA5 Sim --obs "repo ok em 25/11"`
   A nota é recalculada. O valor original do agente fica guardado ao lado, com a data. Se o artigo passar a poder ser aprovado e ainda não tiver extração, ele volta sozinho para a fila em modo `somente_extracao`: rode `/lote` de novo.
3. Revise `revisao.csv` (nota entre 3,0 e 3,5, campos incertos, confiança baixa, possível inelegível, E16 = "Não se aplica", avisos de consistência). Para corrigir um campo:
   `py salvar_extracao.py corrigir A012 E14 "Agente VLM sobre vistas 2D" --obs "revisão"`
   `py salvar_extracao.py corrigir A012 E16 "ScanRefer | Nr3D | Outro" --outro "benchmark próprio"`
   Para refazer o artigo inteiro: `py salvar_extracao.py reabrir A012` (a versão anterior vai para `resultados/historico/`; as sobrescritas de QA são mantidas, as correções de campo não).
4. Rode `exportar` de novo no fim.

**3.3 Estatísticas**
`py estatisticas.py` → abra `relatorios/estatisticas/index.html`. Os PDFs em `graficos/` entram direto no LaTeX. QA é calculado sobre todos os avaliados; a extração, sobre os aprovados. Enquanto ainda houver itens de QA por verificar, `py estatisticas.py --incluir-dependentes` inclui os que dependem de verificação.

## 4. Comandos

| Comando | O que faz |
|---|---|
| `py salvar_extracao.py status` | Recalcula e mostra o progresso. |
| `py salvar_extracao.py exportar` | Consolidado e filas de revisão em `relatorios/`. |
| `py salvar_extracao.py sobrescrever-qa <id> <QA> <Sim\|Parcialmente\|Não> --obs "..."` | Registra a verificação manual de um item de QA. |
| `py salvar_extracao.py corrigir <id> <campo> "<valor>" [--outro "..."] --obs "..."` | Corrige um campo da extração depois da revisão. |
| `py salvar_extracao.py reabrir <id> [--modo somente_extracao]` | Recoloca um artigo na fila. |
| `py salvar_extracao.py pular <id> --motivo "..."` / `retomar-pulado <id>` | Tira ou devolve um artigo da fila (ex.: PDF ilegível). |
| `py concordancia.py amostra` / `comparar` | Conferência cega e concordância. |
| `py estatisticas.py [--incluir-dependentes]` | Gráficos, tabelas e HTML. |

## 5. Problemas comuns

- **Pede permissão a cada comando:** a pasta não foi marcada como confiável. Feche, rode `claude` na pasta de novo e aceite; ou responda "Yes, and don't ask again".
- **`verificar_link` responde "rate limit":** a API do GitHub aceita 60 consultas por hora sem login. O agente marca o item para verificação manual e segue; nada se perde.
- **`ModuleNotFoundError: pymupdf` ou `matplotlib`:** rode o passo 1.1 de novo.
- **CSV abre tudo numa coluna:** os arquivos usam `;`, o padrão do Excel em português. Em outro programa, escolha `;` como separador.
- **Mudou o `CLAUDE.md` no meio:** cada resultado guarda o hash do protocolo usado (`protocol_sha256`). Evite mudar no meio; se mudar algo que afeta artigos já feitos, reabra-os.
