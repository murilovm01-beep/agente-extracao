#!/usr/bin/env python3
"""
concordancia.py - amostra cega de conferencia humana e calculo de concordancia
entre o agente e o pesquisador.

1) Sortear a amostra (uma vez, depois que todos os artigos forem processados):
       py concordancia.py amostra                 15% dos artigos, minimo 10, semente 42
       py concordancia.py amostra --fracao 0.2 --minimo 12
   Cria conferencia/amostra.json, conferencia/amostra.csv (lista dos artigos)
   e um formulario em branco por artigo em conferencia/formularios/<id>.csv.
   Os formularios NAO mostram as respostas do agente.

2) Preencher os formularios lendo os PDFs, sem abrir resultados/ nem os
   relatorios (conferencia cega). Coluna "valor" de cada linha.

3) Comparar:
       py concordancia.py comparar
   Gera em conferencia/: concordancia_por_campo.csv, divergencias.csv e
   resumo_concordancia.txt (kappa de Cohen, concordancia percentual e, nos
   campos de multipla escolha, indice de Jaccard).

Requer apenas a biblioteca padrao do Python.
"""

import argparse
import csv
import datetime as dt
import json
import math
import random
import sys
import unicodedata
from pathlib import Path

import salvar_extracao as se

CONF_DIR = se.ROOT / "conferencia"
FORMS_DIR = CONF_DIR / "formularios"
SAMPLE_PATH = CONF_DIR / "amostra.json"
UNDETERMINED = "Indeterminado"
QA_ORDER = ["Sim", "Parcialmente", "Não"]
DELIM = ";"


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def norm(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    return " ".join(text.casefold().split())


def canonical(raw, allowed):
    """Aceita o valor sem acento ou com outra caixa, se casar com uma unica opcao."""
    raw = raw.strip()
    if raw in allowed:
        return raw
    matches = [a for a in allowed if norm(a) == norm(raw)]
    return matches[0] if len(matches) == 1 else None


def read_csv_rows(path):
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    first = text.splitlines()[0] if text else ""
    delimiter = ";" if first.count(";") >= first.count(",") else ","
    return list(csv.DictReader(text.splitlines(), delimiter=delimiter))


def fmt(value, digits=2):
    if value is None:
        return "n/d"
    return f"{value:.{digits}f}".replace(".", ",")


def landis_koch(kappa):
    if kappa is None:
        return "indefinido"
    if kappa < 0:
        return "pobre"
    for limit, label in ((0.20, "leve"), (0.40, "razoável"), (0.60, "moderada"), (0.80, "substancial")):
        if kappa <= limit:
            return label
    return "quase perfeita"


def cohen_kappa(pairs):
    """pairs: lista de (agente, humano). Devolve (kappa, concordancia)."""
    n = len(pairs)
    if not n:
        return None, None
    po = sum(1 for a, h in pairs if a == h) / n
    cats = {a for a, _ in pairs} | {h for _, h in pairs}
    pe = sum((sum(1 for a, _ in pairs if a == c) / n) * (sum(1 for _, h in pairs if h == c) / n) for c in cats)
    if math.isclose(pe, 1.0):
        return None, po
    return (po - pe) / (1 - pe), po


def weighted_kappa(pairs, order):
    """Kappa ponderado linear para escala ordinal (Sim > Parcialmente > Nao)."""
    pairs = [(a, h) for a, h in pairs if a in order and h in order]
    n = len(pairs)
    if not n:
        return None
    k = len(order)
    idx = {c: i for i, c in enumerate(order)}
    w = lambda i, j: 1 - abs(i - j) / (k - 1)
    po = sum(w(idx[a], idx[h]) for a, h in pairs) / n
    pa = [sum(1 for a, _ in pairs if idx[a] == i) / n for i in range(k)]
    ph = [sum(1 for _, h in pairs if idx[h] == j) / n for j in range(k)]
    pe = sum(w(i, j) * pa[i] * ph[j] for i in range(k) for j in range(k))
    if math.isclose(pe, 1.0):
        return None
    return (po - pe) / (1 - pe)


def jaccard(a, b):
    a, b = set(a), set(b)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# Campos do formulario
# ---------------------------------------------------------------------------

def form_fields(schema):
    fields = []
    for q in schema["qa"]["items"]:
        fields.append({"id": q["id"], "label": q["question"], "type": "qa", "values": QA_ORDER})
    for spec in schema["extraction"]["fields"]:
        if spec.get("auto_from") or spec["type"] == "text":
            continue
        values = ["Sim", "Não"] if spec["type"] == "boolean" else spec["values"]
        fields.append({"id": spec["id"], "label": spec["label"], "type": spec["type"], "values": values,
                       "other_value": spec.get("other_value"), "exclusive": spec.get("exclusive_values", [])})
    return fields


def type_hint(field):
    return {"qa": "escolha única", "single": "escolha única", "boolean": "Sim ou Não",
            "multi": "múltipla: separe com |"}[field["type"]]


# ---------------------------------------------------------------------------
# amostra
# ---------------------------------------------------------------------------

def cmd_sample(args):
    schema = se.load_schema()
    state, results = se.sync_state()
    if SAMPLE_PATH.exists() and not args.refazer:
        sys.exit("A amostra já foi sorteada (conferencia/amostra.json). Para manter a conferência cega e "
                 "reprodutível ela não é sorteada de novo. Use --refazer só se tiver certeza.")
    done = [r for r in state["articles"] if r["id"] in results and r["status"] == se.STATUS_DONE]
    if not done:
        sys.exit("Nenhum artigo concluído ainda.")
    pending = state["counters"]["pendente"] + state["counters"]["em_andamento"]
    if pending:
        print(f"AVISO: ainda há {pending} artigo(s) por processar; o ideal é sortear no fim.")

    total = len(done)
    size = min(total, max(args.minimo, math.ceil(args.fracao * total)))
    rng = random.Random(args.semente)
    # estratifica por "teve extracao" para manter a proporcao de aprovados e reprovados
    strata = {True: [], False: []}
    for r in done:
        strata[bool(results[r["id"]].get("extraction"))].append(r["id"])
    chosen = []
    for key in (True, False):
        ids = sorted(strata[key])
        k = round(size * len(ids) / total)
        chosen += rng.sample(ids, min(k, len(ids)))
    remaining = sorted(set(r["id"] for r in done) - set(chosen))
    while len(chosen) < size and remaining:
        pick = rng.choice(remaining)
        chosen.append(pick)
        remaining.remove(pick)
    chosen = sorted(chosen[:size])

    FORMS_DIR.mkdir(parents=True, exist_ok=True)
    fields = form_fields(schema)
    for article_id in chosen:
        with open(FORMS_DIR / f"{article_id}.csv", "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle, delimiter=DELIM)
            writer.writerow(["campo", "pergunta_ou_rotulo", "tipo", "valores_permitidos", "valor",
                             "texto_outro", "observacao"])
            for f in fields:
                writer.writerow([f["id"], f["label"], type_hint(f), " | ".join(f["values"]), "", "", ""])
    with open(CONF_DIR / "amostra.csv", "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=DELIM)
        writer.writerow(["id", "arquivo_pdf", "formulario"])
        for article_id in chosen:
            record = se.find_record(state, article_id)
            writer.writerow([article_id, f"artigos/{record['file_name']}", f"conferencia/formularios/{article_id}.csv"])
    se.write_json_atomic(SAMPLE_PATH, {
        "created_at": se.now_iso(), "seed": args.semente, "fraction": args.fracao, "minimum": args.minimo,
        "population": total, "ids": chosen,
        "with_extraction": sum(1 for i in chosen if results[i].get("extraction")),
    })
    print(f"Amostra: {len(chosen)} de {total} artigos (semente {args.semente}): {', '.join(chosen)}")
    print("Formulários em conferencia/formularios/. Preencha a coluna 'valor' lendo só o PDF, sem abrir "
          "resultados/ nem relatorios/. Preencha também a extração dos artigos que você reprovaria no QA.")


# ---------------------------------------------------------------------------
# comparar
# ---------------------------------------------------------------------------

def agent_qa_original(item, schema):
    value = item.get("value")
    if value == schema["qa"]["manual_value"]:
        return item.get("provisional_value")
    return value


def parse_human(field, raw, other_text, errors, where):
    raw = (raw or "").strip()
    if not raw:
        return None
    if field["type"] == "multi":
        parts = [p for p in (x.strip() for x in raw.split("|")) if p]
        values = []
        for p in parts:
            c = canonical(p, field["values"])
            if c is None:
                errors.append(f"{where}: '{p}' não é um valor permitido ({' | '.join(field['values'])}).")
                return None
            values.append(c)
        exclusive = [v for v in values if v in field.get("exclusive", [])]
        if exclusive and len(values) > 1:
            errors.append(f"{where}: '{exclusive[0]}' não pode vir junto com outros valores.")
            return None
        if field.get("other_value") in values and not (other_text or "").strip():
            errors.append(f"AVISO {where}: '{field['other_value']}' sem texto em texto_outro (valor aceito).")
        return sorted(set(values))
    c = canonical(raw, field["values"])
    if c is None:
        errors.append(f"{where}: '{raw}' não é um valor permitido ({' | '.join(field['values'])}).")
        return None
    return c


def agent_value(field, result, schema):
    if field["type"] == "qa":
        return agent_qa_original(result["qa"].get(field["id"], {}), schema)
    ext = result.get("extraction")
    if not ext:
        return "__sem_extracao__"
    # o que conta e a resposta do agente, nao a correcao manual feita depois
    corrections = [c for c in result.get("manual_corrections", []) if c.get("field") == field["id"]]
    value = corrections[0]["old"] if corrections else ext.get(field["id"])
    if field["type"] == "boolean":
        return UNDETERMINED if value is None else ("Sim" if value else "Não")
    if field["type"] == "multi":
        return [UNDETERMINED] if value is None else sorted(set(value))
    return UNDETERMINED if value is None else value


def cmd_compare(args):
    schema = se.load_schema()
    sample = se.load_json(SAMPLE_PATH, None)
    if not sample:
        sys.exit("Sorteie a amostra antes: py concordancia.py amostra")
    _, results = se.sync_state(save=False)
    fields = form_fields(schema)
    fmap = {f["id"]: f for f in fields}
    weights = schema["qa"]["answer_weights"]
    threshold = schema["qa"]["threshold"]

    errors, missing_forms = [], []
    pairs = {f["id"]: [] for f in fields}
    divergences = []
    gate_pairs_orig, gate_pairs_final = [], []
    agent_uncertain = 0
    used_articles = 0

    for article_id in sample["ids"]:
        form = FORMS_DIR / f"{article_id}.csv"
        if not form.exists() or article_id not in results:
            missing_forms.append(article_id)
            continue
        rows = read_csv_rows(form)
        human = {}
        for row in rows:
            fid = (row.get("campo") or "").strip()
            if fid not in fmap:
                continue
            value = parse_human(fmap[fid], row.get("valor"), row.get("texto_outro"), errors, f"{article_id} {fid}")
            if value is not None:
                human[fid] = value
        if not human:
            missing_forms.append(article_id)
            continue
        used_articles += 1
        result = results[article_id]

        # gate de QA
        qa_human = [human.get(q["id"]) for q in schema["qa"]["items"]]
        if all(v is not None for v in qa_human):
            human_score = sum(weights[v] for v in qa_human)
            human_gate = "aprovado" if human_score >= threshold else "reprovado"
            agent_orig = sum(weights.get(agent_qa_original(result["qa"].get(q["id"], {}), schema), 0.0)
                             for q in schema["qa"]["items"])
            gate_pairs_orig.append(("aprovado" if agent_orig >= threshold else "reprovado", human_gate))
            final = se.compute_qa_summary(result["qa"], schema)
            final_score = final["score_final"] if final["score_final"] is not None else final["score_provisional"]
            gate_pairs_final.append(("aprovado" if final_score >= threshold else "reprovado", human_gate))

        for f in fields:
            if f["id"] not in human:
                continue
            a = agent_value(f, result, schema)
            if a == "__sem_extracao__":
                continue
            h = human[f["id"]]
            if a == UNDETERMINED or a == [UNDETERMINED]:
                agent_uncertain += 1
            pairs[f["id"]].append((a, h))
            if a != h:
                show = lambda v: " | ".join(v) if isinstance(v, list) else str(v)
                note = ""
                if f["type"] == "qa" and result["qa"].get(f["id"], {}).get("value") == schema["qa"]["manual_value"]:
                    note = "agente marcou verificação manual (comparado o valor provisório)"
                divergences.append([article_id, f["id"], f["label"], show(a), show(h), note])

    # metricas por campo
    out_rows, qa_all, single_all = [], [], []
    for f in fields:
        p = pairs[f["id"]]
        if f["type"] == "multi":
            exact = sum(1 for a, h in p if a == h) / len(p) if p else None
            jac = sum(jaccard(a, h) for a, h in p) / len(p) if p else None
            out_rows.append([f["id"], f["label"], "múltipla", len(p), fmt(exact * 100 if exact is not None else None, 1),
                             "", "", fmt(jac), ""])
            continue
        kappa, po = cohen_kappa(p)
        wk = weighted_kappa(p, QA_ORDER) if f["type"] == "qa" else None
        (qa_all if f["type"] == "qa" else single_all).extend(p)
        out_rows.append([f["id"], f["label"], "QA" if f["type"] == "qa" else "única", len(p),
                         fmt(po * 100 if po is not None else None, 1), fmt(kappa), fmt(wk) if f["type"] == "qa" else "",
                         "", landis_koch(kappa) if p else ""])

    k_qa, po_qa = cohen_kappa(qa_all)
    wk_qa = weighted_kappa(qa_all, QA_ORDER)
    k_single, po_single = cohen_kappa(single_all)
    k_gate, po_gate = cohen_kappa(gate_pairs_orig)
    k_gate_f, po_gate_f = cohen_kappa(gate_pairs_final)

    with open(CONF_DIR / "concordancia_por_campo.csv", "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=DELIM)
        writer.writerow(["campo", "rotulo", "tipo", "pares", "concordancia_pct", "kappa", "kappa_ponderado",
                         "jaccard_medio", "interpretacao_landis_koch"])
        writer.writerows(out_rows)
        writer.writerow([])
        writer.writerow(["QA (todos os itens)", "", "QA", len(qa_all), fmt(po_qa * 100 if po_qa is not None else None, 1),
                         fmt(k_qa), fmt(wk_qa), "", landis_koch(k_qa)])
        writer.writerow(["Decisão de corte (valores originais do agente)", "", "gate", len(gate_pairs_orig),
                         fmt(po_gate * 100 if po_gate is not None else None, 1), fmt(k_gate), "", "", landis_koch(k_gate)])
        writer.writerow(["Decisão de corte (após verificação manual)", "", "gate", len(gate_pairs_final),
                         fmt(po_gate_f * 100 if po_gate_f is not None else None, 1), fmt(k_gate_f), "", "",
                         landis_koch(k_gate_f)])
        writer.writerow(["Campos de escolha única (todos)", "", "única", len(single_all),
                         fmt(po_single * 100 if po_single is not None else None, 1), fmt(k_single), "", "",
                         landis_koch(k_single)])
    with open(CONF_DIR / "divergencias.csv", "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=DELIM)
        writer.writerow(["id", "campo", "rotulo", "valor_agente", "valor_humano", "observacao"])
        writer.writerows(divergences)

    multi_rows = [r for r in out_rows if r[2] == "múltipla" and r[3]]
    lines = [
        f"Concordância agente × pesquisador — {dt.datetime.now().strftime('%d/%m/%Y %H:%M')}",
        f"Amostra: {len(sample['ids'])} artigos (semente {sample['seed']}, {fmt(sample['fraction'] * 100, 0)}% "
        f"de {sample['population']}, mínimo {sample['minimum']}); formulários usados: {used_articles}.",
        "",
        f"QA, todos os itens: concordância {fmt(po_qa * 100 if po_qa is not None else None, 1)}%, "
        f"kappa {fmt(k_qa)} ({landis_koch(k_qa)}), kappa ponderado linear {fmt(wk_qa)}  [{len(qa_all)} pares]",
        f"Decisão de corte 3,5 (valores originais do agente): concordância "
        f"{fmt(po_gate * 100 if po_gate is not None else None, 1)}%, kappa {fmt(k_gate)}  [{len(gate_pairs_orig)} artigos]",
        f"Decisão de corte 3,5 (após verificação manual): concordância "
        f"{fmt(po_gate_f * 100 if po_gate_f is not None else None, 1)}%, kappa {fmt(k_gate_f)}",
        f"Extração, campos de escolha única: concordância "
        f"{fmt(po_single * 100 if po_single is not None else None, 1)}%, kappa {fmt(k_single)} ({landis_koch(k_single)})"
        f"  [{len(single_all)} pares]",
    ]
    if multi_rows:
        lines.append("Extração, múltipla escolha (Jaccard médio / concordância exata): " +
                     "; ".join(f"{r[0]} {r[7]} / {r[4]}%" for r in multi_rows))
    lines += [
        f"Divergências: {len(divergences)} (ver divergencias.csv). Campos em que o agente marcou incerteza: {agent_uncertain}.",
        "",
        "Notas:",
        "- Itens de QA que o agente marcou para verificação manual entram com o valor provisório que ele",
        "  atribuiu (o que o artigo afirma), não com o valor que você confirmou depois.",
        "- 'n/d' = kappa indefinido (todos os pares na mesma categoria); use a concordância percentual.",
        "- Escala de Landis e Koch (1977): ≤0,20 leve; ≤0,40 razoável; ≤0,60 moderada; ≤0,80 substancial; >0,80 quase perfeita.",
    ]
    if missing_forms:
        lines.append(f"- Sem formulário preenchido: {', '.join(missing_forms)}")
    invalid = [e for e in errors if not e.startswith("AVISO ")]
    notices = [e[6:] for e in errors if e.startswith("AVISO ")]
    if invalid:
        lines.append(f"- {len(invalid)} valor(es) ignorado(s) por não serem válidos (corrija e rode de novo):")
        lines += [f"    {e}" for e in invalid]
    if notices:
        lines.append("- Avisos:")
        lines += [f"    {e}" for e in notices]
    (CONF_DIR / "resumo_concordancia.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print("\nArquivos: conferencia/concordancia_por_campo.csv, conferencia/divergencias.csv, "
          "conferencia/resumo_concordancia.txt")


def main():
    parser = argparse.ArgumentParser(description="Amostra cega e concordância agente × pesquisador.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("amostra")
    p.add_argument("--fracao", type=float, default=0.15)
    p.add_argument("--minimo", type=int, default=10)
    p.add_argument("--semente", type=int, default=42)
    p.add_argument("--refazer", action="store_true")
    p.set_defaults(func=cmd_sample)
    sub.add_parser("comparar").set_defaults(func=cmd_compare)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
