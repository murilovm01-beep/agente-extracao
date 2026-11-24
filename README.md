# Agente de extração para revisões sistemáticas

Um agente no [Claude Code](https://code.claude.com) que lê os artigos de uma revisão ou mapeamento sistemático, um por vez, e faz duas etapas:

1. **Avaliação de qualidade (QA):** responde a um checklist de critérios.
2. **Extração de dados:** se o artigo passar na nota de corte, preenche o formulário.

Os scripts em Python validam cada resposta contra um schema, guardam o progresso, separam o que precisa de conferência humana e geram estatísticas e gráficos.

O agente nunca grava resultados direto. Ele preenche um rascunho, e quem valida e grava é o `salvar_extracao.py`. Se o rascunho tiver erro, o script recusa e o agente corrige.

```
PDF ──► agente lê o texto ──► rascunho.json ──► validação (schema) ──► resultados/
                                                        │ recusou? o agente corrige
                                                        ▼
                         conferência humana ◄── filas de revisão ──► estatísticas (gráficos, CSV, HTML)
```

Desenvolvido para o mapeamento sistemático *"Interpretação de instruções espaciais em linguagem natural em cenas 3D (2020–2026)"* (UFSM). O protocolo desse mapeamento está na raiz como exemplo completo e funcional.

## O que ele faz

- **Um artigo por vez, com contexto limpo:** cada artigo roda num subagente separado, para que um não influencie o outro.
- **Validação rígida:** valores fora da lista, campos obrigatórios vazios e justificativas faltando são recusados, com a mensagem do que corrigir.
- **Retomada:** se a execução cair no meio (limite do plano, janela fechada), basta rodar de novo. O artigo em andamento continua de onde parou.
- **Verificação manual explícita:** o que o agente não consegue conferir sozinho, como um link de código, vai para uma fila que você confere depois.
- **Concordância agente × humano:** sorteia uma amostra cega e calcula o κ de Cohen entre as suas respostas e as do agente.
- **Auditoria:** cada resultado guarda o modelo usado, o hash do protocolo e o histórico de correções.
- **Estatísticas:** gráficos (PNG e PDF, prontos para LaTeX), tabelas CSV e um relatório HTML.

## Requisitos

- Python 3.10+
- [Claude Code](https://code.claude.com/docs/en/setup)
- Uma conta do plano Claude (Pro ou Max) **ou** uma chave de API com créditos (veja *Login*)

```bash
pip install -r requirements.txt
```

## Login

O Claude Code aceita duas formas de pagamento. Escolha uma antes de começar.

### Opção 1: conta do plano Claude (Pro/Max)

O uso sai do limite do plano, sem cobrança extra. Se o limite acabar no meio, é só continuar quando ele renovar, porque nada se perde.

1. Confira que a variável `ANTHROPIC_API_KEY` **não** existe. Se ela estiver definida, o Claude Code usa a chave no lugar do plano.
   - Windows (PowerShell): `echo $env:ANTHROPIC_API_KEY` deve sair vazio.
   - Linux/macOS: `echo $ANTHROPIC_API_KEY` deve sair vazio.
2. Na pasta do projeto, rode `claude` e faça login com a sua conta Claude.
3. Rode `/status` e confira que o login está com a conta claude.ai, e não com uma chave de API.

Opcional: para o Claude Code já sugerir esse login, crie `.claude/settings.local.json` com:
```json
{ "forceLoginMethod": "claudeai" }
```

### Opção 2: chave de API (créditos pré-pagos)

O uso é cobrado por token na sua conta do [Claude Console](https://platform.claude.com).

1. No Console, adicione créditos e crie uma chave de API.
2. Defina a variável de ambiente antes de abrir o Claude Code:
   - Windows (PowerShell): `$env:ANTHROPIC_API_KEY = "sk-ant-..."`
   - Linux/macOS: `export ANTHROPIC_API_KEY="sk-ant-..."`
3. Rode `claude` na pasta do projeto, aprove o uso da chave quando ele perguntar e confira com `/status`.

Nunca coloque a chave em arquivos do repositório.

Sobre custo: cada artigo é lido por inteiro, então teste com `/lote 3` e veja o gasto com `/cost` antes de rodar tudo. O custo depende do modelo escolhido e do tamanho dos artigos.

## Uso rápido

> Os comandos usam `py` (Windows). No Linux e no macOS, troque por `python3`.

1. Coloque os PDFs já aprovados na triagem em `artigos/`. Use prefixo numérico (`001_...pdf`), porque a ordem de processamento é alfabética.
2. Rode `claude` na pasta e aceite a pergunta de confiança (*trust*). Sem isso, as permissões do projeto não valem.
3. Escolha o modelo com `/model` e mantenha o mesmo até o fim.
4. Processe em lotes: `/lote 3` para testar, depois `/lote 10`, até aparecer *"Nenhum artigo pendente"*.
5. Acompanhe o progresso a qualquer momento com `py salvar_extracao.py status`.

Depois de processar tudo:

```bash
py concordancia.py amostra        # sorteia a amostra para conferência cega
py concordancia.py comparar       # depois de preencher os formulários
py salvar_extracao.py exportar    # consolidado e filas de revisão em relatorios/
py estatisticas.py                # gráficos, tabelas e relatorios/estatisticas/index.html
```

O passo a passo completo, com conferência manual, correções e solução de problemas, está no **[manual](docs/MANUAL.md)**.

## Usar na sua própria revisão

A ferramenta é guiada por dois arquivos na raiz:

| Arquivo | O que define |
|---|---|
| `schema.json` | Itens de QA, nota de corte, campos da extração e valores permitidos. É o que o validador cobra. |
| `CLAUDE.md` | O protocolo que o agente segue: objetivo da revisão e regras de cada item e campo. |

Para adaptar:

1. Copie `modelos/schema.modelo.json` para `schema.json` e `modelos/CLAUDE.modelo.md` para `CLAUDE.md`, substituindo os de exemplo.
2. Edite os dois seguindo os comentários. O modelo mostra um campo de cada tipo (escolha única, múltipla escolha, booleano, texto e ano automático). Os IDs e valores no `CLAUDE.md` precisam bater com os do `schema.json`.
3. Escreva regras concretas, com exemplos de quando é *Sim*, *Parcialmente* e *Não*. O agente não deduz o que não está escrito.

O que funciona com qualquer schema: validação, notas de QA, controle de estado, filas de revisão, concordância, exportação e um gráfico por campo.

O que é específico do mapeamento de exemplo e é pulado automaticamente com outro schema: os avisos de consistência entre campos (W01–W06) e os gráficos de cruzamento entre campos. Para ter cruzamentos na sua revisão, adapte a função `section_crosses` em `estatisticas.py`.

## Estrutura

```
CLAUDE.md, schema.json     protocolo e schema em uso (exemplo: mapeamento 3D)
salvar_extracao.py         validação, gravação, estado e comandos de correção
ler_pdf.py                 texto, links e imagens de páginas do PDF
verificar_link.py          confere se links de código e dados abrem
concordancia.py            amostra cega e concordância agente × humano
estatisticas.py            gráficos, tabelas e relatório HTML
.claude/                   permissões, subagente extrator e comandos /lote e /proximo
modelos/                   modelos comentados para adaptar a outra revisão
docs/MANUAL.md             manual completo
artigos/                   seus PDFs (não versionados)
trabalho/, resultados/,    rascunhos, resultados e saídas
relatorios/, conferencia/  (não versionados)
```

## Licença

[MIT](LICENSE). Murilo Vargas Martins, 2026.
