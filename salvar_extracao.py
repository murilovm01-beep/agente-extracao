#!/usr/bin/env python3
"""
salvar_extracao.py - validacao, gravacao e estado do mapeamento.

O agente nunca escreve em resultados/ nem no estado.json: ele preenche um
rascunho em trabalho/<id>/rascunho.json e chama este script, que valida contra
o schema.json e so entao grava. O contador e sempre recalculado a partir dos
arquivos em resultados/ e dos PDFs em artigos/, nunca da memoria do agente.

USO (Windows, na pasta do projeto):
    py salvar_extracao.py status                       recalcula e mostra o progresso
    py salvar_extracao.py proximo                      reserva o proximo artigo e cria o rascunho
    py salvar_extracao.py nota     trabalho/A001/rascunho.json   nota de QA e se deve extrair
    py salvar_extracao.py validar  trabalho/A001/rascunho.json   valida sem gravar
    py salvar_extracao.py salvar   trabalho/A001/rascunho.json   valida, grava e conclui o artigo
    py salvar_extracao.py sobrescrever-qa A001 QA6 Sim --obs "link conferido"
    py salvar_extracao.py corrigir A001 E14 "Agente VLM sobre vistas 2D" --obs "revisão"
    py salvar_extracao.py corrigir A001 E16 "ScanRefer | Nr3D | Outro" --outro "Benchmark próprio"
    py salvar_extracao.py reabrir A001 [--modo completo|somente_extracao]
    py salvar_extracao.py pular A001 --motivo "PDF ilegivel"
    py salvar_extracao.py retomar-pulado A001
    py salvar_extracao.py exportar                     consolidado e filas de revisao em relatorios/

Requer apenas a biblioteca padrao do Python.
"""

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schema.json"
STATE_PATH = ROOT / "estado.json"
PROTOCOL_PATH = ROOT / "CLAUDE.md"
ARTICLES_DIR = ROOT / "artigos"
RESULTS_DIR = ROOT / "resultados"
HISTORY_DIR = RESULTS_DIR / "historico"
WORK_DIR = ROOT / "trabalho"
REPORTS_DIR = ROOT / "relatorios"

STATUS_PENDING = "pendente"
STATUS_RUNNING = "em_andamento"
STATUS_DONE = "concluido"
MODE_FULL = "completo"
MODE_EXTRACTION_ONLY = "somente_extracao"

QA_APPROVED = "aprovado"
QA_REJECTED = "reprovado"
QA_DEPENDS = "depende_de_verificacao"

# ";" abre direto em colunas no Excel em portugues
CSV_DELIMITER = ";"


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def today():
    return dt.date.today().isoformat()


def load_json(path, default=None):
    if not Path(path).exists():
        return default
    with open(path, encoding="utf-8-sig") as handle:
        return json.load(handle)


def write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fmt_score(value):
    if value is None:
        return "-"
    text = f"{value:.1f}".replace(".", ",")
    return text


def load_schema():
    schema = load_json(SCHEMA_PATH)
    if schema is None:
        sys.exit(f"ERRO: nao encontrei {SCHEMA_PATH.name} na pasta do projeto.")
    return schema


def field_map(schema):
    return {f["id"]: f for f in schema["extraction"]["fields"]}


def protocol_hash():
    return sha256_file(PROTOCOL_PATH) if PROTOCOL_PATH.exists() else None


# ---------------------------------------------------------------------------
# Nota de QA
# ---------------------------------------------------------------------------

def effective_qa(item, schema):
    """Valor que conta na nota: a sobrescrita manual, se houver; senao o
    valor do agente; None se ainda depende de verificacao manual."""
    override = item.get("manual_override")
    if override and override.get("value"):
        return override["value"]
    value = item.get("value")
    if value == schema["qa"]["manual_value"]:
        return None
    return value


def compute_qa_summary(qa, schema):
    weights = schema["qa"]["answer_weights"]
    threshold = schema["qa"]["threshold"]
    score_min = score_max = score_prov = 0.0
    pending, rejected_parts = [], {"Não": [], "Parcialmente": []}
    for spec in schema["qa"]["items"]:
        item = qa.get(spec["id"], {})
        value = effective_qa(item, schema)
        if value is None:
            pending.append(spec["id"])
            score_max += 1.0
            score_prov += weights.get(item.get("provisional_value"), 0.0)
        else:
            weight = weights.get(value, 0.0)
            score_min += weight
            score_max += weight
            score_prov += weight
            if value in rejected_parts:
                rejected_parts[value].append(spec["id"])
    if score_min >= threshold:
        status = QA_APPROVED
    elif score_max < threshold:
        status = QA_REJECTED
    else:
        status = QA_DEPENDS
    reason = None
    if status == QA_REJECTED:
        parts = [f"nota máxima {fmt_score(score_max)} < {fmt_score(threshold)}"]
        if rejected_parts["Não"]:
            parts.append("Não em " + ", ".join(rejected_parts["Não"]))
        if rejected_parts["Parcialmente"]:
            parts.append("Parcialmente em " + ", ".join(rejected_parts["Parcialmente"]))
        reason = "; ".join(parts)
    return {
        "score_min": round(score_min, 2),
        "score_max": round(score_max, 2),
        "score_provisional": round(score_prov, 2),
        "score_final": round(score_min, 2) if not pending else None,
        "qa_status": status,
        "pending_manual_items": pending,
        "extraction_required": score_max >= threshold,
        "rejection_reason": reason,
    }


# ---------------------------------------------------------------------------
# Validacao do rascunho
# ---------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, rule, message):
        self.errors.append(f"[{rule}] {message}")

    def warn(self, rule, message):
        self.warnings.append(f"[{rule}] {message}")


def valid_uncertain_ids(schema):
    ids = {f["id"] for f in schema["extraction"]["fields"]}
    ids |= {q["id"] for q in schema["qa"]["items"]}
    ids |= set(schema["article"]["uncertain_field_ids_extra"])
    return ids


def validate_metadata(meta, uncertain, schema, rep):
    if not isinstance(meta, dict):
        rep.error("R12", "metadata ausente.")
        return
    spec = schema["metadata"]
    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        if "title" not in uncertain:
            rep.error("R12", "metadata.title vazio.")
    elif len(title) > spec["title"]["max_length"]:
        rep.error("R10", "metadata.title longo demais.")
    authors = meta.get("authors")
    if not isinstance(authors, list) or not [a for a in authors if isinstance(a, str) and a.strip()]:
        if "authors" not in uncertain:
            rep.error("R12", "metadata.authors precisa ser uma lista com pelo menos um nome.")
    venue = meta.get("venue")
    if not isinstance(venue, str) or not venue.strip():
        if "venue" not in uncertain:
            rep.error("R12", "metadata.venue vazio.")
    doi = meta.get("doi")
    if doi is not None and (not isinstance(doi, str) or len(doi) > spec["doi"]["max_length"]):
        rep.error("R12", "metadata.doi deve ser texto (ou null).")
    year = meta.get("year")
    if isinstance(year, str) and year.isdigit():
        year = int(year)
        meta["year"] = year
    if year is None and "year" in uncertain:
        return
    if not isinstance(year, int):
        rep.error("R12", "metadata.year precisa ser um número inteiro (ex.: 2024).")
    elif not (spec["year"]["min"] <= year <= spec["year"]["max"]):
        rep.error("R12", f"metadata.year {year} fora de {spec['year']['min']}-{spec['year']['max']}.")


def validate_qa(qa, uncertain, schema, rep):
    policy = schema["qa"]["justification_policy"]
    limits = schema["qa"]["justification_max_length"]
    allowed = set(schema["qa"]["answer_weights"]) | {schema["qa"]["manual_value"]}
    if not isinstance(qa, dict):
        rep.error("R06", "bloco qa ausente.")
        return
    expected = [q["id"] for q in schema["qa"]["items"]]
    for key in qa:
        if key not in expected and not key.startswith("_"):
            rep.error("R06", f"item de QA desconhecido: {key}.")
    for qid in expected:
        item = qa.get(qid)
        if not isinstance(item, dict):
            rep.error("R06", f"{qid} ausente.")
            continue
        value = item.get("value")
        if value not in allowed:
            rep.error("R06", f"{qid}: valor {value!r} inválido. Use: {', '.join(sorted(allowed))}.")
            continue
        just = item.get("justification")
        rule = policy[value]
        has_just = isinstance(just, str) and just.strip() != ""
        if rule == "forbidden" and has_just:
            rep.error("R06", f"{qid}: não escreva justificativa quando o valor é {value}.")
        if rule == "required" and not has_just:
            rep.error("R06", f"{qid}: justificativa obrigatória (o que foi encontrado e o que falta conferir).")
        if has_just and value in limits and len(just) > limits[value]:
            rep.error("R06", f"{qid}: justificativa com {len(just)} caracteres; máximo {limits[value]}.")
        prov = item.get("provisional_value")
        if value == schema["qa"]["manual_value"]:
            if prov not in schema["qa"]["answer_weights"]:
                rep.error("R07", f"{qid}: informe provisional_value (Sim, Parcialmente ou Não): o que o artigo afirma.")
        elif prov is not None:
            rep.error("R07", f"{qid}: provisional_value só existe quando o valor é {schema['qa']['manual_value']}.")
        extra = set(item) - {"value", "justification", "provisional_value", "manual_override"}
        if extra:
            rep.error("R06", f"{qid}: chaves desconhecidas {sorted(extra)}.")


def manual_field_range(schema):
    """Ex.: 'E01–E19' (campos que o agente preenche, sem os automaticos)."""
    ids = [f["id"] for f in schema["extraction"]["fields"] if not f.get("auto_from")]
    return f"{ids[0]}–{ids[-1]}" if ids else "(nenhum)"


def validate_extraction(ext, other_texts, uncertain, meta, schema, rep):
    fields = field_map(schema)
    other_limit = schema["article"]["other_text_max_length"]
    if not isinstance(ext, dict):
        rep.error("R08", "extraction precisa ser um objeto com os campos " + manual_field_range(schema) + ".")
        return
    for key in ext:
        if key not in fields and not key.startswith("_"):
            rep.error("R01", f"campo desconhecido na extração: {key}.")
    other_texts = other_texts if isinstance(other_texts, dict) else {}
    for fid, spec in fields.items():
        if spec.get("auto_from"):
            continue
        value = ext.get(fid)
        if value is None:
            if fid not in uncertain:
                rep.error("R04", f"{fid} vazio. Preencha, ou inclua {fid} em uncertain_fields se não for possível ter certeza.")
            continue
        kind = spec["type"]
        if kind == "single":
            if value not in spec["values"]:
                rep.error("R01", f"{fid}: {value!r} não é permitido. Opções: {' | '.join(spec['values'])}.")
                continue
            chosen = [value]
        elif kind == "multi":
            if not isinstance(value, list) or not value:
                rep.error("R01", f"{fid}: múltipla escolha deve ser uma lista não vazia.")
                continue
            bad = [v for v in value if v not in spec["values"]]
            if bad:
                rep.error("R01", f"{fid}: {bad} não permitido(s). Opções: {' | '.join(spec['values'])}.")
                continue
            if len(set(value)) != len(value):
                rep.error("R01", f"{fid}: valor repetido.")
            for excl in spec.get("exclusive_values", []):
                if excl in value and len(value) > 1:
                    rep.error("R02", f"{fid}: '{excl}' não pode vir junto com outros valores.")
            chosen = value
        elif kind == "boolean":
            if not isinstance(value, bool):
                rep.error("R01", f"{fid}: use true ou false.")
            chosen = []
        elif kind == "text":
            if not isinstance(value, str) or not value.strip():
                rep.error("R10", f"{fid}: texto vazio (use 'nenhum', 'não reportado' ou 'não se aplica' quando for o caso).")
            elif len(value) > spec["max_length"]:
                rep.error("R10", f"{fid}: {len(value)} caracteres; máximo {spec['max_length']}.")
            chosen = []
        else:
            chosen = []
        other = spec.get("other_value")
        if other:
            if other in chosen:
                text = other_texts.get(fid)
                if not isinstance(text, str) or not text.strip():
                    rep.error("R03", f"{fid} = '{other}': descreva em other_texts.{fid} o que é.")
                elif len(text) > other_limit:
                    rep.error("R03", f"other_texts.{fid}: máximo {other_limit} caracteres.")
            elif fid in other_texts:
                rep.error("R03", f"other_texts.{fid} só existe quando {fid} = '{other}'.")
    for fid in other_texts:
        if fid not in fields or not fields[fid].get("other_value"):
            rep.error("R03", f"other_texts.{fid}: esse campo não tem opção 'Outro/Outra'.")
    # E20 automatico
    for fid, spec in fields.items():
        if spec.get("auto_from") == "metadata.year":
            given = ext.get(fid)
            year = meta.get("year") if isinstance(meta, dict) else None
            if given is not None and year is not None and str(given) != str(year):
                rep.error("R05", f"{fid} ({given}) diferente de metadata.year ({year}). Deixe {fid} de fora: o script preenche.")


def consistency_warnings(ext, schema, rep):
    if not isinstance(ext, dict):
        return
    ex = schema["extraction"]
    # Verificacoes especificas do mapeamento de exemplo (W01-W06). So rodam se o schema
    # declarar as chaves que elas usam; outro schema simplesmente nao recebe esses avisos.
    needed = ("method_only_fields", "image_modalities", "llm_based_mechanisms", "proprietary_model_pattern")
    if not all(k in ex for k in needed) or not {"E01", "E05", "E06", "E08", "E09", "E14", "E16", "E17"} <= set(field_map(schema)):
        return
    na = "Não se aplica"
    kind = ext.get("E01")
    method_fields = ex["method_only_fields"]
    if kind in ("Benchmark ou conjunto de dados", "Estudo de avaliação comparativa"):
        wrong = [f for f in method_fields if ext.get(f) not in (None, na)]
        if wrong:
            rep.warn("W01", f"{kind}: esperava 'Não se aplica' em {', '.join(wrong)}.")
    if kind in ("Método", "Método e benchmark"):
        wrong = [f for f in method_fields if ext.get(f) == na]
        if wrong:
            rep.warn("W02", f"{kind} com 'Não se aplica' em {', '.join(wrong)}.")
    modalities = ext.get("E05") or []
    has_image = any(m in ex["image_modalities"] for m in modalities)
    views = ext.get("E06")
    if views and views != na and not has_image and modalities:
        rep.warn("W03", "E06 informa origem das vistas, mas E05 não tem modalidade de imagem.")
    if views == na and has_image:
        rep.warn("W03", "E05 tem modalidade de imagem, mas E06 = 'Não se aplica'.")
    llm = ext.get("E09")
    pattern = re.compile(ex["proprietary_model_pattern"], re.I)
    if isinstance(llm, str) and pattern.search(llm) and ext.get("E08") is False and kind in ("Método", "Método e benchmark"):
        rep.warn("W04", "E09 cita modelo proprietário, mas E08 = false.")
    if isinstance(llm, str) and llm.strip().lower() in ("nenhum", "nenhum.") and ext.get("E14") in ex["llm_based_mechanisms"]:
        rep.warn("W05", "E09 = 'nenhum', mas o mecanismo em E14 é baseado em LLM/VLM.")
    benchmarks = ext.get("E16") or []
    if ext.get("E17") == na and kind in ("Método", "Método e benchmark") and benchmarks and benchmarks != [na]:
        rep.warn("W06", "E17 = 'Não se aplica' num método avaliado em benchmarks.")


def validate_draft(draft, schema, state, mode_expected=None, check_state=True):
    """Valida o rascunho inteiro. Devolve (report, qa_summary)."""
    rep = Report()
    uncertain = draft.get("uncertain_fields", [])
    if not isinstance(uncertain, list):
        rep.error("R11", "uncertain_fields precisa ser uma lista.")
        uncertain = []
    valid_ids = valid_uncertain_ids(schema)
    bad = [u for u in uncertain if u not in valid_ids]
    if bad:
        rep.error("R11", f"uncertain_fields com identificadores inválidos: {bad}.")
    if len(set(uncertain)) != len(uncertain):
        rep.error("R11", "uncertain_fields com repetição.")
    uncertain = set(uncertain)

    model = draft.get("model")
    if not isinstance(model, str) or not model.strip():
        rep.error("R14", "informe em 'model' o modelo que fez a extração (ex.: claude-opus-...).")

    mode = draft.get("mode", MODE_FULL)
    if check_state:
        record = find_record(state, draft.get("id"))
        if record is None:
            rep.error("R13", f"id {draft.get('id')!r} não está no estado.")
        else:
            if record["status"] != STATUS_RUNNING:
                rep.error("R13", f"{record['id']} não está em andamento (status: {record['status']}). Use 'proximo'.")
            if draft.get("file_sha256") != record["sha256"] or draft.get("file_name") != record["file_name"]:
                rep.error("R13", "arquivo/hash do rascunho não conferem com o artigo em andamento.")
            mode_expected = record.get("reopen_mode") or MODE_FULL
    if mode_expected and mode != mode_expected:
        rep.error("R13", f"modo do rascunho ({mode}) diferente do esperado ({mode_expected}).")

    meta = draft.get("metadata")
    validate_metadata(meta, uncertain, schema, rep)

    if mode == MODE_EXTRACTION_ONLY:
        existing = load_json(RESULTS_DIR / f"{draft.get('id')}.json", {})
        qa = existing.get("qa", {})
        if draft.get("qa"):
            rep.error("R13", "modo somente_extracao: não envie o bloco qa (a avaliação anterior é mantida).")
    else:
        qa = draft.get("qa")
        validate_qa(qa, uncertain, schema, rep)

    summary = None
    if not any(e.startswith("[R06]") or e.startswith("[R07]") for e in rep.errors) and isinstance(qa, dict) and qa:
        summary = compute_qa_summary(qa, schema)
        ext = draft.get("extraction")
        if summary["extraction_required"]:
            if ext is None:
                rep.error("R08", f"nota máxima {fmt_score(summary['score_max'])} >= {fmt_score(schema['qa']['threshold'])}: preencha a extração.")
            else:
                validate_extraction(ext, draft.get("other_texts", {}), uncertain, meta, schema, rep)
                consistency_warnings(ext, schema, rep)
        else:
            filled = isinstance(ext, dict) and any(v is not None for k, v in ext.items() if not k.startswith("_"))
            if ext is not None and (filled or not isinstance(ext, dict)):
                rep.error("R08", f"nota máxima {fmt_score(summary['score_max'])} < {fmt_score(schema['qa']['threshold'])}: deixe extraction = null.")
            if draft.get("other_texts"):
                rep.error("R08", "artigo reprovado: deixe other_texts vazio.")
    elif isinstance(draft.get("extraction"), dict) and any(
            v is not None for k, v in draft["extraction"].items() if not k.startswith("_")):
        # QA com erro: a decisao de extrair fica para depois, mas a extracao
        # ja preenchida e conferida agora, para corrigir tudo de uma vez
        validate_extraction(draft["extraction"], draft.get("other_texts", {}), uncertain, meta, schema, rep)
        consistency_warnings(draft["extraction"], schema, rep)

    conf = draft.get("confidence")
    if conf not in schema["article"]["confidence_values"]:
        rep.error("R11", f"confidence deve ser {' | '.join(schema['article']['confidence_values'])}.")
    elif conf == "alta" and uncertain:
        rep.warn("W07", "confidence = 'alta' com campos incertos.")

    notes = draft.get("notes", "")
    notable = draft.get("notable", False)
    if not isinstance(notes, str):
        rep.error("R09", "notes deve ser texto.")
    else:
        limit = schema["article"]["notes_notable_max_length"] if notable else schema["article"]["notes_max_length"]
        if len(notes) > limit:
            rep.error("R09", f"notes com {len(notes)} caracteres; máximo {limit}"
                      + ("" if notable else " (acima disso, só com notable = true)") + ".")
        if notable and not notes.strip():
            rep.error("R09", "notable = true exige notes.")
    if not isinstance(notable, bool):
        rep.error("R09", "notable deve ser true ou false.")

    elig = draft.get("eligibility_flag")
    if elig is not None:
        reason = elig.get("reason") if isinstance(elig, dict) else None
        if not isinstance(reason, str) or not reason.strip():
            rep.error("R11", "eligibility_flag precisa de {\"reason\": \"...\"} (ou null).")
        elif len(reason) > schema["article"]["eligibility_reason_max_length"]:
            rep.error("R11", "eligibility_flag.reason longo demais (máx. 200).")
    return rep, summary


# ---------------------------------------------------------------------------
# Sinalizacoes para revisao
# ---------------------------------------------------------------------------

def build_flags(result, schema):
    flags = []
    s = result["qa_summary"]
    low, high = schema["qa"]["review_band"]
    reference = s["score_final"] if s["score_final"] is not None else s["score_provisional"]
    if low <= reference < high:
        flags.append({"code": "qa_faixa_3_a_3_5", "message": f"nota {fmt_score(reference)} entre 3,0 e 3,5"})
    if s["qa_status"] == QA_DEPENDS:
        flags.append({"code": "aprovacao_depende_de_verificacao",
                      "message": "aprovação depende de " + ", ".join(s["pending_manual_items"])})
    if result.get("uncertain_fields"):
        flags.append({"code": "campos_incertos", "message": ", ".join(result["uncertain_fields"])})
    if result.get("confidence") == "baixa":
        flags.append({"code": "confianca_baixa", "message": "confiança baixa"})
    if result.get("eligibility_flag"):
        flags.append({"code": "possivel_inelegivel", "message": result["eligibility_flag"]["reason"]})
    ext = result.get("extraction") or {}
    for spec in schema["extraction"]["fields"]:
        for value in spec.get("review_values", []):
            chosen = ext.get(spec["id"])
            if chosen == value or (isinstance(chosen, list) and value in chosen):
                flags.append({"code": f"{spec['id'].lower()}_{value.lower().replace(' ', '_')}",
                              "message": f"{spec['id']} = '{value}'"})
    if result.get("validation_warnings"):
        flags.append({"code": "avisos_de_consistencia", "message": "; ".join(result["validation_warnings"])})
    if result.get("extraction_pending"):
        flags.append({"code": "extracao_pendente", "message": "aprovado após sobrescrita; falta a extração"})
    return flags


def finalize_result(result, schema):
    result["qa_summary"] = compute_qa_summary(result["qa"], schema)
    result["review_flags"] = build_flags(result, schema)
    result["needs_review"] = bool(result["review_flags"])
    return result


# ---------------------------------------------------------------------------
# Estado
# ---------------------------------------------------------------------------

def find_record(state, article_id):
    for record in state.get("articles", []):
        if record["id"] == article_id:
            return record
    return None


def empty_state():
    return {"schema_version": "1.0", "updated_at": None, "articles_dir": "artigos",
            "counters": {}, "warnings": [], "articles": []}


def sync_state(save=True):
    """Reconcilia PDFs, resultados e estado. Fonte da verdade: os arquivos."""
    state = load_json(STATE_PATH, None) or empty_state()
    by_hash = {r["sha256"]: r for r in state.get("articles", [])}
    warnings = []

    # recupera ids a partir dos resultados, caso o estado tenha se perdido
    results = {}
    if RESULTS_DIR.exists():
        for path in sorted(RESULTS_DIR.glob("A*.json")):
            data = load_json(path, None)
            if data and "file_sha256" in data:
                results[data["id"]] = data
                if data["file_sha256"] not in by_hash:
                    by_hash[data["file_sha256"]] = {
                        "id": data["id"], "file_name": data["file_name"], "sha256": data["file_sha256"],
                        "status": STATUS_DONE, "started_at": None, "finished_at": data.get("saved_at"),
                        "reopen_mode": None, "skipped": None}

    pdfs = sorted([p for p in ARTICLES_DIR.glob("*") if p.suffix.lower() == ".pdf"],
                  key=lambda p: p.name.casefold()) if ARTICLES_DIR.exists() else []
    seen_hashes = {}
    present = set()
    used_ids = {r["id"] for r in by_hash.values()}
    next_number = max([int(i[1:]) for i in used_ids if re.fullmatch(r"A\d+", i)] or [0]) + 1

    ordered = []
    for pdf in pdfs:
        digest = sha256_file(pdf)
        if digest in seen_hashes:
            warnings.append(f"'{pdf.name}' é cópia idêntica de '{seen_hashes[digest]}' (mesmo hash) e foi ignorado.")
            continue
        seen_hashes[digest] = pdf.name
        record = by_hash.get(digest)
        if record is None:
            record = {"id": f"A{next_number:03d}", "file_name": pdf.name, "sha256": digest,
                      "status": STATUS_PENDING, "started_at": None, "finished_at": None,
                      "reopen_mode": None, "skipped": None}
            next_number += 1
            by_hash[digest] = record
        elif record["file_name"] != pdf.name:
            warnings.append(f"{record['id']}: arquivo renomeado de '{record['file_name']}' para '{pdf.name}' (mesmo hash, id mantido).")
            record["file_name"] = pdf.name
        record.pop("missing_file", None)
        present.add(digest)
        ordered.append(record)

    for digest, record in by_hash.items():
        if digest not in present:
            record["missing_file"] = True
            if record["id"] in results:
                ordered.append(record)
            else:
                warnings.append(f"{record['id']} ('{record['file_name']}') não está mais em artigos/.")
                ordered.append(record)

    # status derivado dos arquivos
    for record in ordered:
        has_result = record["id"] in results
        if has_result and not record.get("reopen_mode"):
            record["status"] = STATUS_DONE
            record["finished_at"] = results[record["id"]].get("saved_at")
        elif record["status"] == STATUS_RUNNING:
            pass
        else:
            record["status"] = STATUS_PENDING
        if has_result:
            summary = results[record["id"]].get("qa_summary", {})
            record["qa_status"] = summary.get("qa_status")
            final = summary.get("score_final")
            record["score"] = final if final is not None else summary.get("score_provisional")
            record["needs_review"] = results[record["id"]].get("needs_review", False)
            record["pending_manual_items"] = summary.get("pending_manual_items", [])
        else:
            for key in ("qa_status", "score", "needs_review", "pending_manual_items"):
                record.pop(key, None)

    ordered.sort(key=lambda r: (r.get("missing_file", False), r["file_name"].casefold()))
    active = [r for r in ordered if not r.get("missing_file")]
    done = [r for r in active if r["status"] == STATUS_DONE]
    counters = {
        "total": len(active),
        "concluido": len(done),
        "em_andamento": sum(1 for r in active if r["status"] == STATUS_RUNNING),
        "pendente": sum(1 for r in active if r["status"] == STATUS_PENDING and not r.get("skipped")),
        "pulado": sum(1 for r in active if r.get("skipped") and r["status"] != STATUS_DONE),
        "aprovado": sum(1 for r in done if r.get("qa_status") == QA_APPROVED),
        "reprovado": sum(1 for r in done if r.get("qa_status") == QA_REJECTED),
        "depende_de_verificacao": sum(1 for r in done if r.get("qa_status") == QA_DEPENDS),
        "sinalizado_para_revisao": sum(1 for r in done if r.get("needs_review")),
        "com_qa_a_verificar": sum(1 for r in done if r.get("pending_manual_items")),
        "extracao_pendente": sum(1 for r in active if r.get("reopen_mode") == MODE_EXTRACTION_ONLY),
    }
    state.update({"schema_version": "1.0", "updated_at": now_iso(), "articles_dir": "artigos",
                  "counters": counters, "warnings": warnings, "articles": ordered})
    if save:
        write_json_atomic(STATE_PATH, state)
    return state, results


def progress_line(state):
    c = state["counters"]
    remaining = c["pendente"] + c["em_andamento"] + c.get("pulado", 0)
    text = (f"Progresso: {c['concluido']}/{c['total']} concluídos "
            f"(aprovados {c['aprovado']}, reprovados {c['reprovado']}, "
            f"dependem de verificação {c['depende_de_verificacao']}; "
            f"sinalizados {c['sinalizado_para_revisao']}) — faltam {remaining}")
    if c.get("pulado"):
        text += f" ({c['pulado']} pulado(s))"
    return text


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

def cmd_status(args):
    state, _ = sync_state()
    for warning in state["warnings"]:
        print(f"AVISO: {warning}")
    c = state["counters"]
    print(progress_line(state))
    print(f"QA com itens a verificar manualmente: {c['com_qa_a_verificar']} | extração pendente: {c['extracao_pendente']}")
    running = [r for r in state["articles"] if r["status"] == STATUS_RUNNING]
    for r in running:
        print(f"Em andamento: {r['id']} {r['file_name']} (desde {r.get('started_at')})")


def blank_draft(record, schema, existing=None, mode=MODE_FULL):
    fields = [f for f in schema["extraction"]["fields"] if not f.get("auto_from")]
    draft = {
        "_instrucoes": ("Preencha conforme o CLAUDE.md. Depois de preencher o qa, rode "
                        f"'py salvar_extracao.py nota trabalho/{record['id']}/rascunho.json'. "
                        "Ao terminar: 'py salvar_extracao.py salvar trabalho/"
                        f"{record['id']}/rascunho.json'."),
        "id": record["id"],
        "file_name": record["file_name"],
        "file_sha256": record["sha256"],
        "mode": mode,
        "model": "",
        "metadata": {"title": "", "authors": [], "venue": "", "doi": None, "year": None},
    }
    if mode == MODE_FULL:
        draft["qa"] = {q["id"]: {"value": ""} for q in schema["qa"]["items"]}
    else:
        draft["metadata"] = existing.get("metadata", draft["metadata"])
        draft["_qa_atual"] = existing.get("qa_summary")
    draft["extraction"] = {f["id"]: None for f in fields}
    draft["other_texts"] = {}
    draft["eligibility_flag"] = None
    draft["confidence"] = ""
    draft["uncertain_fields"] = []
    draft["notes"] = ""
    draft["notable"] = False
    return draft


def cmd_next(args):
    schema = load_schema()
    state, results = sync_state(save=False)
    candidates = [r for r in state["articles"] if not r.get("missing_file")]
    record = next((r for r in candidates if r["status"] == STATUS_RUNNING), None)
    resumed = record is not None
    if record is None:
        record = next((r for r in candidates if r["status"] == STATUS_PENDING and not r.get("skipped")), None)
    if record is None:
        write_json_atomic(STATE_PATH, state)
        print("Nenhum artigo pendente.")
        print(progress_line(state))
        return
    record["status"] = STATUS_RUNNING
    record["started_at"] = record.get("started_at") if resumed else now_iso()
    write_json_atomic(STATE_PATH, state)
    sync_state()  # recalcula o contador a partir dos arquivos, com o artigo ja reservado

    work = WORK_DIR / record["id"]
    work.mkdir(parents=True, exist_ok=True)
    draft_path = work / "rascunho.json"
    mode = record.get("reopen_mode") or MODE_FULL
    if draft_path.exists() and resumed:
        draft_note = "rascunho existente mantido (retomada): confira e complete"
    else:
        existing = results.get(record["id"], {})
        write_json_atomic(draft_path, blank_draft(record, schema, existing, mode))
        draft_note = "rascunho novo criado"
    rel = lambda p: p.relative_to(ROOT).as_posix()
    print(json.dumps({
        "id": record["id"],
        "file_name": record["file_name"],
        "pdf": rel(ARTICLES_DIR / record["file_name"]),
        "mode": mode,
        "resumed": resumed,
        "draft": rel(draft_path),
        "work_dir": rel(work),
        "draft_note": draft_note,
    }, ensure_ascii=False))


def load_draft(path):
    draft = load_json(path, None)
    if draft is None:
        sys.exit(f"ERRO: não encontrei o rascunho {path}.")
    return draft


def print_report(rep):
    for e in rep.errors:
        print(f"ERRO {e}")
    for w in rep.warnings:
        print(f"AVISO {w}")


def cmd_score(args):
    schema = load_schema()
    draft = load_draft(args.rascunho)
    rep = Report()
    validate_qa(draft.get("qa"), set(draft.get("uncertain_fields", [])), schema, rep)
    if rep.errors:
        print_report(rep)
        sys.exit(1)
    s = compute_qa_summary(draft["qa"], schema)
    print(f"Nota: mínima {fmt_score(s['score_min'])} | máxima {fmt_score(s['score_max'])} | "
          f"provisória {fmt_score(s['score_provisional'])} (de {fmt_score(schema['qa']['max_score'])})")
    print(f"Situação: {s['qa_status']}" + (f" — itens a verificar: {', '.join(s['pending_manual_items'])}"
                                         if s["pending_manual_items"] else ""))
    print("Extração: " + (f"SIM, preencha {manual_field_range(schema)}." if s["extraction_required"]
                          else "NÃO. Deixe extraction = null e other_texts = {}."))


def cmd_validate(args):
    schema = load_schema()
    state, _ = sync_state(save=False)
    draft = load_draft(args.rascunho)
    rep, summary = validate_draft(draft, schema, state)
    print_report(rep)
    if rep.errors:
        print(f"{len(rep.errors)} erro(s). Nada foi gravado.")
        sys.exit(1)
    print("Rascunho válido." + (f" Nota máxima {fmt_score(summary['score_max'])}." if summary else ""))


def cmd_save(args):
    schema = load_schema()
    state, results = sync_state(save=False)
    draft = load_draft(args.rascunho)
    rep, summary = validate_draft(draft, schema, state)
    if rep.errors:
        print_report(rep)
        record = find_record(state, draft.get("id"))
        running = record is not None and record["status"] == STATUS_RUNNING
        print(f"{len(rep.errors)} erro(s). Nada foi gravado" + ("; o artigo continua em andamento." if running else "."))
        sys.exit(1)

    article_id = draft["id"]
    record = find_record(state, article_id)
    mode = draft.get("mode", MODE_FULL)
    previous = results.get(article_id)
    history = list(previous.get("history", [])) if previous else []
    if previous and mode == MODE_FULL:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(RESULTS_DIR / f"{article_id}.json", HISTORY_DIR / f"{article_id}_{stamp}.json")
        history.append({"date": now_iso(), "action": "reprocessado", "details": "versão anterior em resultados/historico"})

    extraction = draft.get("extraction")
    if not (summary and summary["extraction_required"]):
        extraction = None  # reprovado: rascunho em branco vira null
    if extraction is not None:
        extraction = {k: v for k, v in extraction.items() if not k.startswith("_")}
        for spec in schema["extraction"]["fields"]:
            if spec.get("auto_from") == "metadata.year":
                year = draft["metadata"].get("year")
                extraction[spec["id"]] = str(year) if year is not None else None

    if mode == MODE_EXTRACTION_ONLY:
        result = previous
        old_uncertain = [u for u in result.get("uncertain_fields", []) if u.startswith("QA")]
        result["uncertain_fields"] = old_uncertain + [u for u in draft.get("uncertain_fields", []) if not u.startswith("QA")]
        result["extraction"] = extraction
        result["extraction_pending"] = False
        result["metadata"] = draft["metadata"]
        history.append({"date": now_iso(), "action": "extracao_apos_sobrescrita", "details": draft.get("model")})
    else:
        result = {
            "schema_version": schema["schema_version"],
            "id": article_id,
            "file_name": draft["file_name"],
            "file_sha256": draft["file_sha256"],
            "metadata": draft["metadata"],
            "qa": {k: {kk: vv for kk, vv in v.items()} for k, v in draft["qa"].items() if not k.startswith("_")},
            "extraction": extraction,
            "uncertain_fields": draft.get("uncertain_fields", []),
        }
        for item in result["qa"].values():
            item["manual_override"] = None
        kept = []
        if previous:
            # verificacoes manuais feitas pelo pesquisador continuam valendo no reprocessamento
            for qid, old_item in previous.get("qa", {}).items():
                if old_item.get("manual_override") and qid in result["qa"]:
                    result["qa"][qid]["manual_override"] = old_item["manual_override"]
                    kept.append(qid)
        history.append({"date": now_iso(), "action": "extraido",
                        "details": draft.get("model") + (f"; sobrescritas mantidas: {', '.join(kept)}" if kept else "")})
        if previous and previous.get("manual_corrections"):
            fields_fixed = sorted({c["field"] for c in previous["manual_corrections"]})
            print(f"AVISO: correções manuais da versão anterior não foram reaplicadas ({', '.join(fields_fixed)}); "
                  "confira e use 'corrigir' de novo se preciso.")

    result.update({
        "model": draft["model"],
        "protocol_sha256": protocol_hash(),
        "saved_at": now_iso(),
        "other_texts": {k: v for k, v in (draft.get("other_texts") or {}).items()},
        "eligibility_flag": draft.get("eligibility_flag"),
        "confidence": draft["confidence"],
        "notes": draft.get("notes", ""),
        "notable": draft.get("notable", False),
        "validation_warnings": rep.warnings,
        "history": history,
    })
    result.setdefault("extraction_pending", False)
    finalize_result(result, schema)
    write_json_atomic(RESULTS_DIR / f"{article_id}.json", result)

    record["reopen_mode"] = None
    record["status"] = STATUS_DONE
    write_json_atomic(STATE_PATH, state)
    state, _ = sync_state()
    print_summary(result, state, schema)


def print_summary(result, state, schema):
    s = result["qa_summary"]
    ref = s["score_final"] if s["score_final"] is not None else s["score_provisional"]
    label = {"aprovado": "aprovado", "reprovado": "reprovado",
             "depende_de_verificacao": "depende de verificação"}[s["qa_status"]]
    score_txt = fmt_score(ref) + ("" if s["score_final"] is not None
                                  else f" provisória, faixa {fmt_score(s['score_min'])}–{fmt_score(s['score_max'])}")
    extraction = "sim" if result.get("extraction") else "não"
    print(f"[{result['id']}] {result['file_name']} — QA {score_txt}/{fmt_score(schema['qa']['max_score'])} ({label}) — extração: {extraction}")
    flags = [f"{f['code']}: {f['message']}" for f in result["review_flags"]]
    if s["pending_manual_items"]:
        print("Verificar manualmente: " + ", ".join(s["pending_manual_items"]))
    print("Sinalizações: " + (" | ".join(flags) if flags else "nenhuma"))
    if s.get("rejection_reason"):
        print(f"Motivo: {s['rejection_reason']}")
    print(progress_line(state))


def cmd_override(args):
    schema = load_schema()
    path = RESULTS_DIR / f"{args.id}.json"
    result = load_json(path, None)
    if result is None:
        sys.exit(f"ERRO: {args.id} ainda não tem resultado salvo.")
    if args.item not in result["qa"]:
        sys.exit(f"ERRO: item {args.item} inexistente.")
    if args.valor not in schema["qa"]["answer_weights"]:
        sys.exit(f"ERRO: valor deve ser {', '.join(schema['qa']['answer_weights'])}.")
    before = compute_qa_summary(result["qa"], schema)
    result["qa"][args.item]["manual_override"] = {"value": args.valor, "date": today(), "note": args.obs or ""}
    after = compute_qa_summary(result["qa"], schema)
    result.setdefault("history", []).append({
        "date": now_iso(), "action": "sobrescrita_qa",
        "details": f"{args.item} = {args.valor}" + (f" ({args.obs})" if args.obs else "")})

    state, _ = sync_state(save=False)
    record = find_record(state, args.id)
    message = ""
    if after["extraction_required"] and not result.get("extraction"):
        already = record.get("reopen_mode") == MODE_EXTRACTION_ONLY
        result["extraction_pending"] = True
        record["reopen_mode"] = MODE_EXTRACTION_ONLY
        if record["status"] != STATUS_RUNNING:
            record["status"] = STATUS_PENDING
        message = ("Continua na fila em modo somente_extracao." if already else
                   "O artigo passou a poder ser aprovado: voltou para a fila em modo somente_extracao.")
    elif not after["extraction_required"] and result.get("extraction_pending"):
        result["extraction_pending"] = False
        if record is not None and record.get("reopen_mode") == MODE_EXTRACTION_ONLY:
            record["reopen_mode"] = None
        message = "O artigo voltou a reprovado: saiu da fila de extração."
    elif not after["extraction_required"] and result.get("extraction"):
        message = "O artigo passou a reprovado: a extração fica guardada, mas sai das estatísticas."
    finalize_result(result, schema)
    write_json_atomic(path, result)
    write_json_atomic(STATE_PATH, state)
    state, _ = sync_state()
    print(f"{args.id} {args.item}: {args.valor}. Nota {fmt_score(before['score_provisional'])} -> "
          f"{fmt_score(after['score_final'] if after['score_final'] is not None else after['score_provisional'])}"
          f" ({after['qa_status']}).")
    if after["pending_manual_items"]:
        print("Ainda a verificar: " + ", ".join(after["pending_manual_items"]))
    if message:
        print(message)
    print(progress_line(state))


def parse_cli_value(spec, raw):
    kind = spec["type"]
    if kind == "multi":
        return [p.strip() for p in raw.split("|") if p.strip()]
    if kind == "boolean":
        low = raw.strip().casefold()
        if low in ("sim", "true", "s"):
            return True
        if low in ("não", "nao", "false", "n"):
            return False
        return raw
    return raw.strip()


def cmd_correct(args):
    """Correcao manual de um campo da extracao, depois da revisao humana."""
    schema = load_schema()
    fields = field_map(schema)
    path = RESULTS_DIR / f"{args.id}.json"
    result = load_json(path, None)
    if result is None:
        sys.exit(f"ERRO: {args.id} ainda não tem resultado salvo.")
    if not result.get("extraction"):
        sys.exit(f"ERRO: {args.id} não tem extração (reprovado no QA ou extração pendente).")
    spec = fields.get(args.campo)
    if spec is None or spec.get("auto_from"):
        sys.exit(f"ERRO: campo {args.campo} inválido (E20 vem do ano; para metadados, reabra o artigo).")
    new = parse_cli_value(spec, args.valor)
    ext = dict(result["extraction"])
    old = ext.get(args.campo)
    if old == new and not args.outro:
        sys.exit(f"{args.id} {args.campo} já é {old!r}. Nada foi alterado.")
    ext[args.campo] = new
    other_texts = dict(result.get("other_texts") or {})
    other = spec.get("other_value")
    if args.outro:
        other_texts[args.campo] = args.outro
    elif other and not (new == other or (isinstance(new, list) and other in new)):
        other_texts.pop(args.campo, None)
    uncertain = [u for u in result.get("uncertain_fields", []) if u != args.campo]
    rep = Report()
    validate_extraction(ext, other_texts, set(uncertain), result["metadata"], schema, rep)
    if rep.errors:
        print_report(rep)
        sys.exit("Nada foi alterado.")
    consistency_warnings(ext, schema, rep)
    if result.get("confidence") == "alta" and uncertain:
        rep.warn("W07", "confidence = 'alta' com campos incertos.")
    result.update({"extraction": ext, "other_texts": other_texts, "uncertain_fields": uncertain,
                   "validation_warnings": rep.warnings})
    result.setdefault("manual_corrections", []).append(
        {"field": args.campo, "old": old, "new": new, "date": today(), "note": args.obs or ""})
    result.setdefault("history", []).append(
        {"date": now_iso(), "action": "correcao_manual",
         "details": f"{args.campo}: {old!r} -> {new!r}" + (f" ({args.obs})" if args.obs else "")})
    finalize_result(result, schema)
    write_json_atomic(path, result)
    state, _ = sync_state()
    print(f"{args.id} {args.campo}: {old!r} -> {new!r}.")
    for w in rep.warnings:
        print(f"AVISO {w}")
    print(progress_line(state))


def cmd_reopen(args):
    state, results = sync_state(save=False)
    record = find_record(state, args.id)
    if record is None:
        sys.exit(f"ERRO: {args.id} não existe.")
    if args.id not in results:
        sys.exit(f"ERRO: {args.id} ainda não foi concluído.")
    if args.modo == MODE_EXTRACTION_ONLY and not compute_qa_summary(results[args.id]["qa"], load_schema())["extraction_required"]:
        sys.exit("ERRO: com a nota atual o artigo não pode ser aprovado; sobrescreva o QA antes, ou reabra em modo completo.")
    record["reopen_mode"] = args.modo
    record["status"] = STATUS_PENDING
    draft = WORK_DIR / args.id / "rascunho.json"
    if draft.exists():
        draft.unlink()
    write_json_atomic(STATE_PATH, state)
    state, _ = sync_state()
    print(f"{args.id} reaberto em modo {args.modo}. Ele é o próximo da fila na ordem dos nomes.")


def cmd_skip(args):
    state, results = sync_state(save=False)
    record = find_record(state, args.id)
    if record is None:
        sys.exit(f"ERRO: {args.id} não existe.")
    if args.id in results and not record.get("reopen_mode"):
        sys.exit(f"ERRO: {args.id} já está concluído.")
    record["skipped"] = {"reason": args.motivo, "date": now_iso()}
    record["status"] = STATUS_PENDING
    write_json_atomic(STATE_PATH, state)
    state, _ = sync_state()
    print(f"{args.id} pulado: {args.motivo}. Para voltar: py salvar_extracao.py retomar-pulado {args.id}")
    print(progress_line(state))


def cmd_unskip(args):
    state, _ = sync_state(save=False)
    record = find_record(state, args.id)
    if record is None:
        sys.exit(f"ERRO: {args.id} não existe.")
    record["skipped"] = None
    write_json_atomic(STATE_PATH, state)
    sync_state()
    print(f"{args.id} voltou para a fila.")


# ---------------------------------------------------------------------------
# Exportacao
# ---------------------------------------------------------------------------

def flatten_value(value):
    if isinstance(value, list):
        return "; ".join(value)
    if isinstance(value, bool):
        return "sim" if value else "não"
    return "" if value is None else str(value)


def cmd_export(args):
    schema = load_schema()
    state, results = sync_state()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ordered = [results[r["id"]] for r in state["articles"] if r["id"] in results]
    write_json_atomic(REPORTS_DIR / "consolidado.json",
                      {"exported_at": now_iso(), "counters": state["counters"], "articles": ordered})

    qa_ids = [q["id"] for q in schema["qa"]["items"]]
    field_ids = [f["id"] for f in schema["extraction"]["fields"]]
    header = (["id", "file_name", "title", "authors", "venue", "doi", "year", "qa_status",
               "score_final", "score_provisional", "score_min", "score_max"]
              + [f"{q}" for q in qa_ids] + field_ids
              + ["other_texts", "confidence", "uncertain_fields", "needs_review", "notes", "notable", "model"])
    with open(REPORTS_DIR / "consolidado.csv", "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=CSV_DELIMITER)
        writer.writerow(header)
        for r in ordered:
            s = r["qa_summary"]
            meta = r["metadata"]
            qa_values = []
            for q in qa_ids:
                item = r["qa"][q]
                value = effective_qa(item, schema)
                qa_values.append(value if value is not None else f"verificar ({item.get('provisional_value')})")
            ext = r.get("extraction") or {}
            writer.writerow([r["id"], r["file_name"], meta.get("title"), "; ".join(meta.get("authors") or []),
                             meta.get("venue"), meta.get("doi") or "", meta.get("year"), s["qa_status"],
                             s["score_final"], s["score_provisional"], s["score_min"], s["score_max"]]
                            + qa_values + [flatten_value(ext.get(f)) for f in field_ids]
                            + [json.dumps(r.get("other_texts") or {}, ensure_ascii=False), r.get("confidence"),
                               "; ".join(r.get("uncertain_fields") or []), r.get("needs_review"),
                               r.get("notes"), r.get("notable"), r.get("model")])

    with open(REPORTS_DIR / "outros.csv", "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=CSV_DELIMITER)
        writer.writerow(["campo", "id", "titulo", "texto"])
        rows = []
        for r in ordered:
            for fid, text in (r.get("other_texts") or {}).items():
                rows.append([fid, r["id"], r["metadata"].get("title"), text])
        for row in sorted(rows, key=lambda x: (x[0], x[3].casefold())):
            writer.writerow(row)

    with open(REPORTS_DIR / "qa_para_verificar.csv", "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=CSV_DELIMITER)
        writer.writerow(["id", "arquivo", "item", "valor_provisorio", "o_que_conferir",
                         "aprovacao_depende", "comando_para_registrar"])
        for r in ordered:
            s = r["qa_summary"]
            for q in s["pending_manual_items"]:
                item = r["qa"][q]
                writer.writerow([r["id"], r["file_name"], q, item.get("provisional_value"), item.get("justification"),
                                 "sim" if s["qa_status"] == QA_DEPENDS else "não",
                                 f'py salvar_extracao.py sobrescrever-qa {r["id"]} {q} <Sim|Parcialmente|Não> --obs "..."'])

    with open(REPORTS_DIR / "revisao.csv", "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=CSV_DELIMITER)
        writer.writerow(["id", "arquivo", "titulo", "qa_status", "nota", "confianca", "sinalizacoes", "notes"])
        for r in ordered:
            if not r.get("needs_review"):
                continue
            s = r["qa_summary"]
            nota = s["score_final"] if s["score_final"] is not None else s["score_provisional"]
            writer.writerow([r["id"], r["file_name"], r["metadata"].get("title"), s["qa_status"], nota,
                             r.get("confidence"), " | ".join(f"{f['code']}: {f['message']}" for f in r["review_flags"]),
                             r.get("notes")])

    print(f"Exportado em {REPORTS_DIR.relative_to(ROOT).as_posix()}/: consolidado.json, consolidado.csv, "
          "outros.csv (textos de 'Outro'), qa_para_verificar.csv, revisao.csv")
    print(progress_line(state))


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Validação, gravação e estado do mapeamento.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("proximo").set_defaults(func=cmd_next)
    for name, func in (("nota", cmd_score), ("validar", cmd_validate), ("salvar", cmd_save)):
        p = sub.add_parser(name)
        p.add_argument("rascunho")
        p.set_defaults(func=func)
    p = sub.add_parser("sobrescrever-qa")
    p.add_argument("id")
    p.add_argument("item")
    p.add_argument("valor")
    p.add_argument("--obs", default="")
    p.set_defaults(func=cmd_override)
    p = sub.add_parser("corrigir")
    p.add_argument("id")
    p.add_argument("campo")
    p.add_argument("valor", help="múltipla escolha: separe com | ; E08: Sim ou Não")
    p.add_argument("--outro", default="", help="descrição quando o valor é Outro/Outra")
    p.add_argument("--obs", default="")
    p.set_defaults(func=cmd_correct)
    p = sub.add_parser("reabrir")
    p.add_argument("id")
    p.add_argument("--modo", choices=[MODE_FULL, MODE_EXTRACTION_ONLY], default=MODE_FULL)
    p.set_defaults(func=cmd_reopen)
    p = sub.add_parser("pular")
    p.add_argument("id")
    p.add_argument("--motivo", required=True)
    p.set_defaults(func=cmd_skip)
    p = sub.add_parser("retomar-pulado")
    p.add_argument("id")
    p.set_defaults(func=cmd_unskip)
    sub.add_parser("exportar").set_defaults(func=cmd_export)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
