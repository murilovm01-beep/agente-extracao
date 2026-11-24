#!/usr/bin/env python3
"""
estatisticas.py - graficos, tabelas e relatorio HTML do mapeamento.

Le os resultados validados em resultados/ (nunca o estado da memoria do
agente) e o schema.json, e gera em relatorios/estatisticas/:
    graficos/   PNG (para ver) e PDF (vetorial, para o LaTeX)
    tabelas/    CSV de cada grafico, separados por ";" (abrem direto no Excel)
    index.html  relatorio com todos os graficos e as tabelas

Bases de calculo:
    - Avaliacao de qualidade: todos os artigos avaliados.
    - Extracao: so os aprovados no QA (nota minima >= 3,5). Com
      --incluir-dependentes, entram tambem os que dependem de verificacao
      manual (a extracao deles ja foi feita).

USO (na pasta do projeto):
    py estatisticas.py
    py estatisticas.py --incluir-dependentes
    py estatisticas.py --formatos png          (so PNG)

Requer: py -m pip install matplotlib
"""

import argparse
import csv
import datetime as dt
import html
import json
import math
import sys
import textwrap
from collections import Counter, OrderedDict
from pathlib import Path

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
except ImportError:
    sys.exit("ERRO: falta o matplotlib. Instale com: py -m pip install matplotlib")

import salvar_extracao as se

# ---------------------------------------------------------------------------
# Paleta e estilo (paleta de referencia validada; ver LEIA-ME)
# ---------------------------------------------------------------------------

SURFACE = "#ffffff"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
ORDINAL_QA = {"Sim": "#184f95", "Parcialmente": "#3987e5", "Não": "#86b6ef", "A verificar": "#c3c2b7"}
ORDINAL_TEXT = {"Sim": "#ffffff", "Parcialmente": "#ffffff", "Não": INK, "A verificar": INK}
SEQUENTIAL = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5",
              "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]
NEUTRAL_FILL = "#a8a69f"

UNDETERMINED = "Indeterminado"
NEUTRAL_VALUES = {"Não se aplica", "Não informado", UNDETERMINED}
OTHER_VALUES = {"Outro", "Outra"}
METHOD_TYPES = {"Método", "Método e benchmark"}

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 8.5,
    "axes.edgecolor": AXIS,
    "axes.labelcolor": INK_2,
    "xtick.color": INK_2,
    "ytick.color": INK_2,
    "xtick.labelcolor": INK_2,
    "ytick.labelcolor": INK_2,
    "axes.facecolor": SURFACE,
    "figure.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "legend.frameon": False,
    "legend.fontsize": 8,
    "pdf.fonttype": 42,
})

FIG_WIDTH = 6.3  # polegadas, largura de texto de uma pagina A4


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def pct(part, whole):
    return 0.0 if not whole else 100.0 * part / whole


def fmt_pct(value):
    text = f"{value:.1f}".replace(".", ",")
    return text[:-2] if text.endswith(",0") else text


def fmt_num(value):
    if isinstance(value, float):
        return f"{value:.1f}".replace(".", ",")
    return str(value)


def wrap(label, width=36):
    return "\n".join(textwrap.wrap(str(label), width, break_long_words=False)) or str(label)


def as_list(value):
    if value is None:
        return [UNDETERMINED]
    if isinstance(value, bool):
        return ["Sim" if value else "Não"]
    if isinstance(value, list):
        return value or [UNDETERMINED]
    return [value]


def year_of(result):
    year = (result.get("metadata") or {}).get("year")
    return str(year) if year is not None else UNDETERMINED


def style_axes(ax, grid_axis="x"):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    if grid_axis == "x":
        ax.spines["left"].set_visible(False)
        ax.tick_params(axis="y", length=0)
    else:
        ax.tick_params(axis="x", length=0)
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def set_title(fig, text):
    fig.suptitle(text, x=0.01, ha="left", fontsize=10, fontweight="bold", color=INK)


def note_text(fig, ax, text):
    """Nota de rodape logo abaixo de tudo o que o eixo desenha (rotulos e legenda)."""
    if not text:
        return
    box = ax.get_tightbbox(fig.canvas.get_renderer()).transformed(fig.transFigure.inverted())
    fig.text(0.01, box.y0 - 0.02, text, ha="left", va="top", fontsize=7.2, color=INK_2)


# ---------------------------------------------------------------------------
# Relatorio (acumula graficos e tabelas para o HTML)
# ---------------------------------------------------------------------------

class Report:
    def __init__(self, out_dir, formats):
        self.out = Path(out_dir)
        self.charts = self.out / "graficos"
        self.tables = self.out / "tabelas"
        self.charts.mkdir(parents=True, exist_ok=True)
        self.tables.mkdir(parents=True, exist_ok=True)
        self.formats = formats
        self.sections = OrderedDict()

    def save_fig(self, fig, name):
        for ext in self.formats:
            fig.savefig(self.charts / f"{name}.{ext}", dpi=200 if ext == "png" else None, bbox_inches="tight")
        plt.close(fig)

    def save_table(self, name, header, rows):
        with open(self.tables / f"{name}.csv", "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(header)
            for row in rows:
                writer.writerow([fmt_num(v) if isinstance(v, float) else v for v in row])

    def add(self, section, name, title, caption, header, rows, chart=True):
        self.save_table(name, header, rows)
        self.sections.setdefault(section, []).append(
            {"name": name, "title": title, "caption": caption, "header": header, "rows": rows,
             "chart": chart and "png" in self.formats})


# ---------------------------------------------------------------------------
# Tipos de grafico
# ---------------------------------------------------------------------------

def order_options(spec_values, counts):
    """Opcoes reais por frequencia (empate: ordem do schema); depois Outro,
    Nao informado, Nao se aplica e Indeterminado."""
    position = {v: i for i, v in enumerate(spec_values)}
    regular = [v for v in counts if v not in OTHER_VALUES | NEUTRAL_VALUES]
    regular.sort(key=lambda v: (-counts[v], position.get(v, 999)))
    tail = [v for v in ["Outro", "Outra", "Não informado", "Não se aplica", UNDETERMINED] if v in counts]
    return regular + tail


def chart_hbar(report, name, title, labels, counts, n_base, note=None):
    wrapped = [wrap(l, 46) for l in labels]
    lines = max(w.count("\n") + 1 for w in wrapped)
    height = 0.75 + (0.30 + 0.16 * (lines - 1)) * len(labels)
    fig, ax = plt.subplots(figsize=(FIG_WIDTH, height))
    y = list(range(len(labels)))[::-1]
    values = [counts[l] for l in labels]
    colors = [NEUTRAL_FILL if l in NEUTRAL_VALUES else BLUE for l in labels]
    ax.barh(y, values, height=0.62, color=colors, edgecolor=SURFACE, linewidth=1)
    ax.set_yticks(y, wrapped)
    top = max(values + [1])
    ax.set_xlim(0, top * 1.22 + 0.5)
    for yi, v in zip(y, values):
        ax.text(v + top * 0.015, yi, f"{v} ({fmt_pct(pct(v, n_base))}%)", va="center", fontsize=7.8, color=INK_2)
    ax.xaxis.get_major_locator().set_params(integer=True)
    ax.set_xlabel("Número de estudos")
    style_axes(ax, "x")
    set_title(fig, title)
    fig.tight_layout()
    if note:
        note_text(fig, ax, note)
    report.save_fig(fig, name)


def chart_vbar(report, name, title, labels, values, value_labels, ylabel, note=None, ylim=None):
    fig, ax = plt.subplots(figsize=(FIG_WIDTH, 2.9))
    x = list(range(len(labels)))
    colors = [NEUTRAL_FILL if l in NEUTRAL_VALUES else BLUE for l in labels]
    ax.bar(x, values, width=0.6, color=colors, edgecolor=SURFACE, linewidth=1)
    ax.set_xticks(x, labels)
    top = ylim if ylim else max(values + [1]) * 1.18
    ax.set_ylim(0, top)
    for xi, v, lab in zip(x, values, value_labels):
        ax.text(xi, v + top * 0.015, lab, ha="center", va="bottom", fontsize=7.8, color=INK_2)
    if not ylim:
        ax.yaxis.get_major_locator().set_params(integer=True)
    ax.set_ylabel(ylabel)
    style_axes(ax, "y")
    set_title(fig, title)
    fig.tight_layout()
    if note:
        note_text(fig, ax, note)
    report.save_fig(fig, name)


def chart_stacked_vbar(report, name, title, x_labels, series, matrix, ylabel, colors, note=None):
    fig, ax = plt.subplots(figsize=(FIG_WIDTH, 3.2))
    x = list(range(len(x_labels)))
    bottoms = [0] * len(x_labels)
    for s_idx, s_name in enumerate(series):
        vals = [matrix[s_idx][j] for j in range(len(x_labels))]
        ax.bar(x, vals, bottom=bottoms, width=0.6, color=colors[s_idx], edgecolor=SURFACE,
               linewidth=1.2, label=s_name)
        bottoms = [b + v for b, v in zip(bottoms, vals)]
    top = max(bottoms + [1]) * 1.15
    ax.set_ylim(0, top)
    for xi, total in zip(x, bottoms):
        if total:
            ax.text(xi, total + top * 0.015, str(total), ha="center", va="bottom", fontsize=7.8, color=INK_2)
    ax.set_xticks(x, x_labels)
    ax.yaxis.get_major_locator().set_params(integer=True)
    ax.set_ylabel(ylabel)
    style_axes(ax, "y")
    set_title(fig, title)
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.13), ncol=2, handlelength=1, columnspacing=1.2)
    fig.tight_layout()
    if note:
        note_text(fig, ax, note)
    report.save_fig(fig, name)


def chart_grouped_vbar(report, name, title, x_labels, series, matrix, labels, ylabel, colors, ylim=None, note=None):
    fig, ax = plt.subplots(figsize=(FIG_WIDTH, 3.0))
    k = len(series)
    width = 0.8 / k
    top = ylim or max([v for row in matrix for v in row] + [1]) * 1.18
    for s_idx, s_name in enumerate(series):
        xs = [j + (s_idx - (k - 1) / 2) * width for j in range(len(x_labels))]
        ax.bar(xs, matrix[s_idx], width=width * 0.92, color=colors[s_idx], edgecolor=SURFACE,
               linewidth=1, label=s_name)
        for xi, v, lab in zip(xs, matrix[s_idx], labels[s_idx]):
            ax.text(xi, v + top * 0.015, lab, ha="center", va="bottom", fontsize=7, color=INK_2)
    ax.set_ylim(0, top)
    ax.set_xticks(range(len(x_labels)), x_labels)
    ax.set_ylabel(ylabel)
    style_axes(ax, "y")
    set_title(fig, title)
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.26), ncol=k, handlelength=1)
    fig.tight_layout()
    if note:
        note_text(fig, ax, note)
    report.save_fig(fig, name)


def chart_stacked_hbar_pct(report, name, title, row_labels, categories, matrix, colors, text_colors, note=None):
    height = 1.1 + 0.36 * len(row_labels)
    fig, ax = plt.subplots(figsize=(FIG_WIDTH, height))
    y = list(range(len(row_labels)))[::-1]
    lefts = [0.0] * len(row_labels)
    for c_idx, cat in enumerate(categories):
        vals = [matrix[r][c_idx] for r in range(len(row_labels))]
        ax.barh(y, vals, left=lefts, height=0.62, color=colors[cat], edgecolor=SURFACE, linewidth=1.2, label=cat)
        for yi, left, v in zip(y, lefts, vals):
            if v >= 7:
                ax.text(left + v / 2, yi, f"{fmt_pct(v)}%", ha="center", va="center", fontsize=7.2,
                        color=text_colors[cat])
        lefts = [l + v for l, v in zip(lefts, vals)]
    ax.set_yticks(y, row_labels)
    ax.set_xlim(0, 100)
    ax.set_xticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
    style_axes(ax, "x")
    set_title(fig, title)
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.1 * 3.6 / height), ncol=len(categories), handlelength=1)
    fig.tight_layout()
    if note:
        note_text(fig, ax, note)
    report.save_fig(fig, name)


def chart_heatmap(report, name, title, row_labels, col_labels, matrix, xlabel, ylabel, note=None):
    cmap = LinearSegmentedColormap.from_list("seq", SEQUENTIAL)
    height = 1.1 + 0.32 * len(row_labels)
    width = min(FIG_WIDTH, 2.6 + 0.62 * len(col_labels))
    fig, ax = plt.subplots(figsize=(max(width, 4.2), height))
    top = max([v for row in matrix for v in row] + [1])
    for i, row in enumerate(matrix):
        yi = len(row_labels) - 1 - i
        for j, v in enumerate(row):
            if v:
                level = 0.15 + 0.85 * (v / top)
                color = cmap(level)
                text_color = "#ffffff" if level > 0.5 else INK
            else:
                color, text_color = "#f4f3ef", MUTED
            ax.add_patch(plt.Rectangle((j - 0.5, yi - 0.5), 1, 1, facecolor=color, edgecolor=SURFACE, linewidth=2))
            ax.text(j, yi, str(v) if v else "·", ha="center", va="center", fontsize=7.6, color=text_color)
    ax.set_xlim(-0.5, len(col_labels) - 0.5)
    ax.set_ylim(-0.5, len(row_labels) - 0.5)
    ax.set_xticks(range(len(col_labels)), [wrap(c, 14) for c in col_labels])
    ax.set_yticks(range(len(row_labels)), [wrap(r, 34) for r in row_labels][::-1])
    ax.tick_params(length=0)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    set_title(fig, title)
    fig.tight_layout()
    if note:
        note_text(fig, ax, note)
    report.save_fig(fig, name)


def chart_bubble(report, name, title, row_labels, col_labels, matrix, xlabel, ylabel, note=None):
    height = 1.3 + 0.42 * len(row_labels)
    fig, ax = plt.subplots(figsize=(FIG_WIDTH, height))
    top = max([v for row in matrix for v in row] + [1])
    max_area = 1300
    for i, row in enumerate(matrix):
        yi = len(row_labels) - 1 - i
        for j, v in enumerate(row):
            if not v:
                continue
            area = max_area * v / top
            ax.scatter(j, yi, s=area, color=BLUE, alpha=0.9, edgecolors=SURFACE, linewidths=2, zorder=3)
            radius_pts = math.sqrt(area / math.pi)
            if radius_pts >= 7:
                ax.text(j, yi, str(v), ha="center", va="center", fontsize=7.6, color="#ffffff", zorder=4)
            else:
                ax.annotate(str(v), (j, yi), xytext=(radius_pts + 3, 0), textcoords="offset points",
                            va="center", fontsize=7.6, color=INK_2, zorder=4)
    ax.set_xlim(-0.6, len(col_labels) - 0.4)
    ax.set_ylim(-0.6, len(row_labels) - 0.4)
    ax.set_xticks(range(len(col_labels)), [wrap(c, 22) for c in col_labels])
    ax.set_yticks(range(len(row_labels)), [wrap(r, 34) for r in row_labels][::-1])
    ax.tick_params(length=0)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.grid(color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    set_title(fig, title)
    fig.tight_layout()
    if note:
        note_text(fig, ax, note)
    report.save_fig(fig, name)


# ---------------------------------------------------------------------------
# Dados
# ---------------------------------------------------------------------------

def load_results(schema):
    results = []
    if not se.RESULTS_DIR.exists():
        return results
    for path in sorted(se.RESULTS_DIR.glob("A*.json")):
        data = se.load_json(path, None)
        if not data or "qa" not in data:
            continue
        data["qa_summary"] = se.compute_qa_summary(data["qa"], schema)  # recalcula com sobrescritas
        results.append(data)
    return results


def options_with_presence(spec, values_seen):
    """Opcoes do schema mais Indeterminado, se apareceu."""
    base = list(spec.get("values", []))
    if spec["type"] == "boolean":
        base = ["Sim", "Não"]
    if UNDETERMINED in values_seen and UNDETERMINED not in base:
        base.append(UNDETERMINED)
    return base


def count_field(studies, spec):
    counts = Counter()
    seen = set()
    for r in studies:
        for v in as_list((r.get("extraction") or {}).get(spec["id"])):
            counts[v] += 1
            seen.add(v)
    options = options_with_presence(spec, seen)
    for o in options:
        counts.setdefault(o, 0)
    return counts, options


def cross(studies, fid_rows, fid_cols, rows, cols):
    matrix = [[0] * len(cols) for _ in rows]
    row_pos = {v: i for i, v in enumerate(rows)}
    col_pos = {v: j for j, v in enumerate(cols)}
    for r in studies:
        ext = r.get("extraction") or {}
        row_vals = as_list(ext.get(fid_rows)) if fid_rows != "year" else [year_of(r)]
        col_vals = as_list(ext.get(fid_cols)) if fid_cols != "year" else [year_of(r)]
        for rv in row_vals:
            for cv in col_vals:
                if rv in row_pos and cv in col_pos:
                    matrix[row_pos[rv]][col_pos[cv]] += 1
    return matrix


def year_axis(items, schema):
    """Todos os anos do protocolo, na ordem (anos sem estudo aparecem como lacuna),
    mais Indeterminado se algum estudo ficou sem ano."""
    year_fields = [f for f in schema["extraction"]["fields"] if f.get("auto_from") == "metadata.year"]
    if year_fields and year_fields[0].get("values"):
        years = list(year_fields[0]["values"])
    else:
        years = sorted({year_of(r) for r in items if year_of(r) != UNDETERMINED})
    if any(year_of(r) == UNDETERMINED for r in items):
        years.append(UNDETERMINED)
    return years


def present_values(studies, fid, base):
    seen = set()
    for r in studies:
        vals = as_list((r.get("extraction") or {}).get(fid)) if fid != "year" else [year_of(r)]
        seen.update(vals)
    return [v for v in base if v in seen] + sorted(v for v in seen if v not in base)


def drop_empty(rows, matrix):
    keep = [i for i, row in enumerate(matrix) if any(row)]
    return [rows[i] for i in keep], [matrix[i] for i in keep]


# ---------------------------------------------------------------------------
# Secoes
# ---------------------------------------------------------------------------

def section_summary(report, results, studies, include_dependents):
    statuses = Counter(r["qa_summary"]["qa_status"] for r in results)
    rows = [
        ["Artigos avaliados (QA)", len(results)],
        ["Aprovados (nota mínima ≥ 3,5)", statuses.get(se.QA_APPROVED, 0)],
        ["Reprovados (nota máxima < 3,5)", statuses.get(se.QA_REJECTED, 0)],
        ["Dependem de verificação manual", statuses.get(se.QA_DEPENDS, 0)],
        ["Base das estatísticas de extração", len(studies)],
        ["Inclui os que dependem de verificação?", "sim" if include_dependents else "não"],
    ]
    report.add("Resumo", "00_resumo", "Resumo", "", ["Indicador", "Valor"], rows, chart=False)


def section_qa(report, results, schema):
    n = len(results)
    weights = schema["qa"]["answer_weights"]
    threshold = schema["qa"]["threshold"]

    # distribuicao das notas
    bins = [i / 2 for i in range(0, int(schema["qa"]["max_score"] * 2) + 1)]
    final, provisional = Counter(), Counter()
    for r in results:
        s = r["qa_summary"]
        if s["score_final"] is not None:
            final[s["score_final"]] += 1
        else:
            provisional[s["score_provisional"]] += 1
    fig, ax = plt.subplots(figsize=(FIG_WIDTH, 3.0))
    x = list(range(len(bins)))
    v_final = [final.get(b, 0) for b in bins]
    v_prov = [provisional.get(b, 0) for b in bins]
    ax.bar(x, v_final, width=0.7, color=BLUE, edgecolor=SURFACE, linewidth=1, label="Nota final")
    ax.bar(x, v_prov, bottom=v_final, width=0.7, color="#86b6ef", edgecolor=SURFACE, linewidth=1,
           label="Nota provisória (itens a verificar)")
    totals = [a + b for a, b in zip(v_final, v_prov)]
    top = max(totals + [1]) * 1.2
    for xi, t in zip(x, totals):
        if t:
            ax.text(xi, t + top * 0.015, str(t), ha="center", va="bottom", fontsize=7.8, color=INK_2)
    cut_x = bins.index(threshold) - 0.5
    ax.axvline(cut_x, color=INK_2, linestyle=(0, (4, 3)), linewidth=1)
    ax.text(cut_x + 0.12, top * 0.93, f"corte: ≥ {fmt_num(threshold)}", fontsize=7.6, color=INK_2, va="top")
    ax.set_ylim(0, top)
    ax.set_xticks(x, [fmt_num(b) for b in bins])
    ax.yaxis.get_major_locator().set_params(integer=True)
    ax.set_xlabel("Nota de qualidade (0 a 7)")
    ax.set_ylabel("Número de estudos")
    style_axes(ax, "y")
    set_title(fig, f"Distribuição das notas de qualidade (n = {n})")
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.2), ncol=2, handlelength=1)
    fig.tight_layout()
    report.save_fig(fig, "01_qa_distribuicao_notas")
    rows = [[fmt_num(b), final.get(b, 0), provisional.get(b, 0), final.get(b, 0) + provisional.get(b, 0)] for b in bins]
    report.add("Avaliação de qualidade", "01_qa_distribuicao_notas", "Distribuição das notas de qualidade",
               "Notas de 0 a 7 em passos de 0,5. A linha tracejada marca o corte de 3,5: abaixo dele o estudo "
               "não segue para a extração. A nota provisória usa o valor afirmado no artigo para os itens que "
               "ainda dependem de verificação manual.",
               ["nota", "estudos_nota_final", "estudos_nota_provisoria", "total"], rows)

    # itens de QA
    cats = ["Sim", "Parcialmente", "Não", "A verificar"]
    labels, matrix, rows = [], [], []
    for q in schema["qa"]["items"]:
        counter = Counter()
        for r in results:
            value = se.effective_qa(r["qa"].get(q["id"], {}), schema)
            counter[value if value in weights else "A verificar"] += 1
        labels.append(f"{q['id']} · {q.get('short', q['question'])}")
        matrix.append([pct(counter[c], n) for c in cats])
        rows.append([q["id"], q["question"]] + [counter[c] for c in cats] + [pct(counter[c], n) for c in cats])
    present = [c for c in cats if any(m[cats.index(c)] for m in matrix)] or cats[:3]
    sub = [[m[cats.index(c)] for c in present] for m in matrix]
    chart_stacked_hbar_pct(report, "02_qa_por_item", f"Respostas por item de qualidade (n = {n})",
                           labels, present, sub, ORDINAL_QA, ORDINAL_TEXT)
    report.add("Avaliação de qualidade", "02_qa_por_item", "Respostas por item de qualidade",
               "Percentual de estudos avaliados em cada resposta. Sobrescritas manuais já aplicadas; "
               "'A verificar' são itens que ainda aguardam conferência manual.",
               ["item", "pergunta"] + [f"n_{c}" for c in cats] + [f"pct_{c}" for c in cats], rows)

    # disponibilidade por ano (so no schema de exemplo, que tem QA5 = codigo e QA6 = dados)
    if not {"QA5", "QA6"} <= {item["id"] for item in schema["qa"]["items"]}:
        return
    years = year_axis(results, schema)
    per_year = {y: [r for r in results if year_of(r) == y] for y in years}
    series = ["QA5 = Sim (código)", "QA6 = Sim (dados e pesos)"]
    mat, lab, rows = [[], []], [[], []], []
    for y in years:
        group = per_year[y]
        row = [y, len(group)]
        for s_idx, qid in enumerate(["QA5", "QA6"]):
            k = sum(1 for r in group if se.effective_qa(r["qa"].get(qid, {}), schema) == "Sim")
            mat[s_idx].append(pct(k, len(group)))
            lab[s_idx].append(f"{k}/{len(group)}" if group else "–")
            row += [k, pct(k, len(group))]
        rows.append(row)
    chart_grouped_vbar(report, "03_qa_disponibilidade_por_ano",
                       "Estudos com código e artefatos acessíveis, por ano",
                       [f"{y}\n(n={len(per_year[y])})" for y in years], series, mat, lab,
                       "% dos estudos avaliados", CATEGORICAL[:2], ylim=118)
    report.add("Avaliação de qualidade", "03_qa_disponibilidade_por_ano",
               "Disponibilidade de código (QA5) e de dados e pesos (QA6) por ano",
               "Percentual dos estudos avaliados de cada ano com resposta 'Sim'. Itens ainda a verificar não "
               "contam como 'Sim'.",
               ["ano", "estudos", "qa5_sim", "pct_qa5_sim", "qa6_sim", "pct_qa6_sim"], rows)


def section_fields(report, studies, schema):
    fmap = se.field_map(schema)
    n = len(studies)
    for fid in schema["extraction"]["chart_fields"]:
        spec = fmap[fid]
        counts, options = count_field(studies, spec)
        multi = spec["type"] == "multi"
        note = ("Múltipla escolha: um estudo pode aparecer em mais de uma opção; os percentuais são sobre "
                f"os {n} estudos.") if multi else None
        title = f"{fid} · {spec['label']} (n = {n})"
        name = f"campo_{fid}"
        is_year = spec.get("auto_from") == "metadata.year"
        if is_year:
            labels = options
            values = [counts[l] for l in labels]
            chart_vbar(report, name, title, labels, values,
                       [f"{v}" for v in values], "Número de estudos")
        else:
            labels = order_options(options, counts)
            chart_hbar(report, name, title, labels, counts, n, note)
        rows = [[l, counts[l], pct(counts[l], n)] for l in (options if is_year else labels)]
        report.add("Campos da extração", name, f"{fid} · {spec['label']}",
                   (note or "") + (" " if note else "") +
                   "'Indeterminado' = campo marcado como incerto e deixado vazio pelo extrator.",
                   ["opcao", "estudos", "pct_dos_estudos"], rows)


def section_crosses(report, studies, schema):
    fmap = se.field_map(schema)
    # Cruzamentos escritos para o mapeamento de exemplo; com outro schema, esta secao e pulada.
    if not {"E01", "E08", "E12", "E13", "E14", "E15", "E16", "E17"} <= set(fmap):
        return
    n = len(studies)
    years_all = year_axis(studies, schema)
    methods = [r for r in studies if (r.get("extraction") or {}).get("E01") in METHOD_TYPES]
    n_m = len(methods)

    # E01 x E20
    types = fmap["E01"]["values"]
    matrix = cross(studies, "E01", "year", types, years_all)
    chart_stacked_vbar(report, "cruz_E01_E20", f"Tipo de contribuição por ano (n = {n})",
                       years_all, types, matrix, "Número de estudos", CATEGORICAL)
    report.add("Cruzamentos", "cruz_E01_E20", "Tipo de contribuição (E01) por ano (E20)", "",
               ["E01"] + years_all, [[t] + row for t, row in zip(types, matrix)])

    # E14 x E15 (bolhas, so metodos)
    rows_v = present_values(methods, "E14", fmap["E14"]["values"])
    cols_v = present_values(methods, "E15", fmap["E15"]["values"])
    matrix = cross(methods, "E14", "E15", rows_v, cols_v)
    chart_bubble(report, "cruz_E14_E15", f"Mecanismo de grounding × raciocínio espacial (métodos, n = {n_m})",
                 rows_v, cols_v, matrix, "E15 · Raciocínio espacial", "")
    report.add("Cruzamentos", "cruz_E14_E15", "Mecanismo de grounding (E14) × raciocínio espacial (E15)",
               "Só estudos de método (E01 = Método ou Método e benchmark). Área da bolha proporcional ao número "
               "de estudos.", ["E14"] + cols_v, [[r] + row for r, row in zip(rows_v, matrix)])

    # E16 x E20 (mapa de calor)
    rows_v = present_values(studies, "E16", fmap["E16"]["values"])
    matrix = cross(studies, "E16", "year", rows_v, years_all)
    chart_heatmap(report, "cruz_E16_E20", f"Benchmarks usados por ano (n = {n})", rows_v, years_all, matrix,
                  "Ano", "", "Múltipla escolha: um estudo conta em cada benchmark que usa.")
    report.add("Cruzamentos", "cruz_E16_E20", "Benchmarks e conjuntos de dados (E16) por ano (E20)",
               "Número de estudos que usam cada benchmark em cada ano. Um estudo conta em cada benchmark que usa.",
               ["E16"] + years_all, [[r] + row for r, row in zip(rows_v, matrix)])

    # E16 x E17 (mapa de calor)
    rows_v = present_values(studies, "E16", fmap["E16"]["values"])
    cols_v = present_values(studies, "E17", fmap["E17"]["values"])
    matrix = cross(studies, "E16", "E17", rows_v, cols_v)
    chart_heatmap(report, "cruz_E16_E17", f"Benchmark × caixas/rótulos na avaliação (n = {n})",
                  rows_v, cols_v, matrix, "E17 · Caixas/rótulos na avaliação", "",
                  "Múltipla escolha em E16: um estudo conta em cada benchmark que usa.")
    report.add("Cruzamentos", "cruz_E16_E17", "Benchmarks (E16) × caixas/rótulos na avaliação (E17)",
               "Mostra em que condição (rótulos preditos ou ground truth) cada benchmark é reportado — o ponto "
               "central da comparabilidade.", ["E16"] + cols_v, [[r] + row for r, row in zip(rows_v, matrix)])

    # E08 x E20 (% por ano)
    labels, values, vlabels, rows = [], [], [], []
    for y in years_all:
        group = [r for r in studies if year_of(r) == y]
        k = sum(1 for r in group if (r.get("extraction") or {}).get("E08") is True)
        u = sum(1 for r in group if (r.get("extraction") or {}).get("E08") is None)
        labels.append(y)
        values.append(pct(k, len(group)))
        vlabels.append(f"{k}/{len(group)}" if group else "–")
        rows.append([y, len(group), k, pct(k, len(group)), u])
    chart_vbar(report, "cruz_E08_E20", f"Estudos que dependem de modelo ou API proprietária, por ano (n = {n})",
               labels, values, vlabels, "% dos estudos do ano", ylim=118)
    report.add("Cruzamentos", "cruz_E08_E20", "Dependência de modelo ou API proprietária (E08) por ano",
               "Percentual dos estudos de cada ano com E08 = sim; o rótulo mostra estudos com dependência / "
               "estudos do ano.", ["ano", "estudos", "dependem", "pct_dependem", "indeterminado"], rows)

    # E12 x E14 e E13 x E14 (bolhas, so metodos)
    for fid, label in (("E12", "Tratamento de ambiguidade referencial"), ("E13", "Tratamento de ponto de vista")):
        rows_v = present_values(methods, "E14", fmap["E14"]["values"])
        cols_v = present_values(methods, fid, fmap[fid]["values"])
        matrix = cross(methods, "E14", fid, rows_v, cols_v)
        name = f"cruz_{fid}_E14"
        note = "Múltipla escolha em E12: um estudo conta em cada tipo de ambiguidade que trata." if fid == "E12" else None
        chart_bubble(report, name, f"Mecanismo de grounding × {label.lower()} (métodos, n = {n_m})",
                     rows_v, cols_v, matrix, f"{fid} · {label}", "", note)
        report.add("Cruzamentos", name, f"Mecanismo de grounding (E14) × {label.lower()} ({fid})",
                   "Só estudos de método. " + (note or ""), ["E14"] + cols_v,
                   [[r] + row for r, row in zip(rows_v, matrix)])


def section_texts(report, studies, schema):
    fmap = se.field_map(schema)
    rows = []
    for r in studies:
        for fid, text in sorted((r.get("other_texts") or {}).items()):
            rows.append([fid, r["id"], (r.get("metadata") or {}).get("title", ""), text])
    rows.sort(key=lambda x: (x[0], str(x[3]).casefold()))
    report.add("Textos livres", "outros_textos", "Descrições de 'Outra' (E02) e 'Outro' (E14, E16)",
               "Base para revisar a taxonomia: textos parecidos podem virar uma categoria nova.",
               ["campo", "id", "titulo", "texto"], rows, chart=False)

    desc = schema["extraction"]["descriptive_fields"]
    rows = []
    for r in studies:
        ext = r.get("extraction") or {}
        rows.append([r["id"], (r.get("metadata") or {}).get("title", ""), year_of(r)] +
                    [ext.get(f) if ext.get(f) is not None else UNDETERMINED for f in desc])
    report.add("Textos livres", "campos_descritivos", "Campos descritivos (" + ", ".join(desc) + ")",
               "Não entram em gráficos: servem para a síntese narrativa.",
               ["id", "titulo", "ano"] + [f"{f} {fmap[f]['label']}" for f in desc], rows, chart=False)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def write_html(report, generated_at, include_dependents):
    esc = html.escape
    parts = []
    nav = []
    for s_idx, (section, items) in enumerate(report.sections.items()):
        anchor = f"s{s_idx}"
        nav.append(f'<a href="#{anchor}">{esc(section)}</a>')
        parts.append(f'<section id="{anchor}"><h2>{esc(section)}</h2>')
        for item in items:
            parts.append('<article class="card">')
            parts.append(f"<h3>{esc(item['title'])}</h3>")
            if item["chart"]:
                parts.append(f'<img src="graficos/{esc(item["name"])}.png" alt="{esc(item["title"])}" loading="lazy">')
            if item["caption"]:
                parts.append(f'<p class="caption">{esc(item["caption"])}</p>')
            table = ['<div class="table-wrap"><table><thead><tr>']
            table += [f"<th>{esc(str(h))}</th>" for h in item["header"]]
            table.append("</tr></thead><tbody>")
            for row in item["rows"]:
                cells = []
                for v in row:
                    text = fmt_pct(v) if isinstance(v, float) else ("" if v is None else str(v))
                    cls = ' class="num"' if isinstance(v, (int, float)) and not isinstance(v, bool) else ""
                    cells.append(f"<td{cls}>{esc(text)}</td>")
                table.append("<tr>" + "".join(cells) + "</tr>")
            table.append("</tbody></table></div>")
            open_attr = "" if item["chart"] else " open"
            parts.append(f"<details{open_attr}><summary>Tabela ({len(item['rows'])} linhas) · "
                         f'<a href="tabelas/{esc(item["name"])}.csv">CSV</a></summary>{"".join(table)}</details>')
            parts.append("</article>")
        parts.append("</section>")

    page = f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Estatísticas do mapeamento</title>
<style>
:root {{
  --surface: #fcfcfb; --page: #f9f9f7; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --ring: rgba(11,11,11,0.10); --accent: #2a78d6;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
header, main {{ max-width: 980px; margin: 0 auto; padding: 0 16px; }}
header {{ padding-top: 28px; }}
h1 {{ font-size: 1.6rem; margin: 0 0 4px; }}
.meta {{ color: var(--ink-2); margin: 0 0 16px; }}
nav {{ display: flex; flex-wrap: wrap; gap: 6px 14px; padding: 10px 0 18px; border-bottom: 1px solid var(--grid); }}
nav a {{ color: var(--accent); text-decoration: none; }}
h2 {{ font-size: 1.25rem; margin: 32px 0 12px; }}
h3 {{ font-size: 1rem; margin: 0 0 10px; }}
.card {{ background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 16px; margin: 0 0 16px; }}
img {{ display: block; max-width: 100%; height: auto; margin: 0 auto 8px; }}
.caption {{ color: var(--ink-2); font-size: 0.9rem; margin: 6px 0 8px; }}
details summary {{ cursor: pointer; color: var(--ink-2); font-size: 0.9rem; }}
.table-wrap {{ overflow-x: auto; margin-top: 8px; }}
table {{ border-collapse: collapse; font-size: 0.85rem; width: 100%; }}
th, td {{ text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }}
th {{ color: var(--ink-2); font-weight: 600; }}
td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
footer {{ color: var(--muted); font-size: 0.85rem; max-width: 980px; margin: 24px auto 40px; padding: 0 16px; }}
</style>
</head>
<body>
<header>
<h1>Estatísticas do mapeamento sistemático</h1>
<p class="meta">Gerado em {esc(generated_at)} a partir de resultados/ ·
QA sobre todos os artigos avaliados · extração sobre os aprovados{' e os que dependem de verificação' if include_dependents else ''}.</p>
<nav>{''.join(nav)}</nav>
</header>
<main>
{''.join(parts)}
</main>
<footer>Gráficos em graficos/ (PNG e PDF) e tabelas em tabelas/ (CSV separado por ponto e vírgula).</footer>
</body>
</html>
"""
    (report.out / "index.html").write_text(page, encoding="utf-8")


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Gráficos e tabelas do mapeamento.")
    parser.add_argument("--incluir-dependentes", action="store_true",
                        help="inclui na extração os artigos cuja aprovação depende de verificação manual")
    parser.add_argument("--saida", default=str(se.REPORTS_DIR / "estatisticas"))
    parser.add_argument("--formatos", default="png,pdf", help="png, pdf ou png,pdf")
    args = parser.parse_args()

    schema = se.load_schema()
    results = load_results(schema)
    if not results:
        print("Nenhum resultado em resultados/. Processe artigos antes de gerar as estatísticas.")
        return
    allowed = {se.QA_APPROVED} | ({se.QA_DEPENDS} if args.incluir_dependentes else set())
    studies = [r for r in results if r["qa_summary"]["qa_status"] in allowed and r.get("extraction")]

    formats = [f.strip().lower() for f in args.formatos.split(",") if f.strip()]
    report = Report(args.saida, formats)
    section_summary(report, results, studies, args.incluir_dependentes)
    section_qa(report, results, schema)
    if studies:
        section_fields(report, studies, schema)
        section_crosses(report, studies, schema)
        section_texts(report, studies, schema)
    else:
        print("Nenhum artigo aprovado com extração ainda: só os gráficos de QA foram gerados.")
    generated_at = dt.datetime.now().strftime("%d/%m/%Y %H:%M")
    write_html(report, generated_at, args.incluir_dependentes)
    out = Path(args.saida)
    try:
        out_txt = out.relative_to(se.ROOT).as_posix()
    except ValueError:
        out_txt = str(out)
    print(f"Estatísticas geradas em {out_txt}/ — abra index.html no navegador.")
    print(f"QA: {len(results)} artigo(s) avaliado(s) | extração: {len(studies)} artigo(s) na base.")


if __name__ == "__main__":
    main()
