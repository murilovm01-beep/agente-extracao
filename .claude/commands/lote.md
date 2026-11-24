---
description: Processa vários artigos em sequência, um subagente por artigo (contexto limpo para cada um)
argument-hint: [quantidade]
---

Processe até $ARGUMENTS artigos (se nenhum número foi informado, 5).

Para cada artigo, **um de cada vez, nunca em paralelo nem em segundo plano** (o estado só admite um artigo em andamento):

1. Use o subagente `extrator` com a instrução "Processe o próximo artigo pendente do mapeamento." e **espere o relatório dele** antes de qualquer outra ação.
2. Mostre ao usuário as linhas de resumo que ele devolveu.
3. Pare antes de completar a quantidade se:
   - ele responder "Nenhum artigo pendente.";
   - ele relatar um erro que não conseguiu resolver;
   - o artigo tiver sido pulado.

Você só coordena: não leia PDFs, rascunhos nem resultados, e não rode `proximo` você mesmo.

No fim, rode `py salvar_extracao.py status` e mostre a linha de progresso.
