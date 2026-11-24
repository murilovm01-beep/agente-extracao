#!/usr/bin/env python3
"""
ler_pdf.py - leitura economica de PDFs para o agente.

Extrair o texto pagina a pagina custa muito menos tokens do que mandar o PDF
inteiro como imagem. Uma pagina so vira imagem quando uma tabela ou figura
sai ilegivel no texto.

USO (Windows, na pasta do projeto):
    py ler_pdf.py texto  artigos/arquivo.pdf trabalho/A001/texto.txt
    py ler_pdf.py links  artigos/arquivo.pdf
    py ler_pdf.py imagem artigos/arquivo.pdf 6 trabalho/A001/p6.png
    py ler_pdf.py imagem artigos/arquivo.pdf 6 trabalho/A001/p6_tab.png --recorte 0,0.45,1,0.8 --zoom 3

    --recorte x0,y0,x1,y1 : fracoes da pagina (0 a 1), origem no canto superior esquerdo
    --zoom                : 2 = 144 dpi (padrao); 3 para tabelas pequenas

Requer PyMuPDF:  py -m pip install pymupdf
"""

import argparse
import re
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24
except ImportError:
    try:
        import fitz  # versoes antigas do PyMuPDF
    except ImportError:
        sys.exit("ERRO: PyMuPDF não está instalado. Rode: py -m pip install pymupdf")

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
REFERENCES_RE = re.compile(r"^\s*(references|bibliography|referências)\s*$", re.I | re.M)
TABLE_RE = re.compile(r"^\s*(table|tab\.)\s*\d+", re.I | re.M)
FIGURE_RE = re.compile(r"^\s*(figure|fig\.)\s*\d+", re.I | re.M)

CATEGORIES = [
    ("codigo", re.compile(r"github\.com/|gitlab\.com/|bitbucket\.org/|codeocean", re.I)),
    ("hugging_face", re.compile(r"huggingface\.co/", re.I)),
    ("armazenamento", re.compile(r"drive\.google|docs\.google|sharepoint|onedrive|1drv\.ms|dropbox|"
                                 r"pan\.baidu|zenodo|figshare|box\.com|mega\.nz|osf\.io|kaggle", re.I)),
    ("pagina_projeto", re.compile(r"github\.io|\.io/|sites\.google|project", re.I)),
    ("artigo_ou_doi", re.compile(r"doi\.org|arxiv\.org|openaccess\.thecvf|aclanthology|ieeexplore|"
                                 r"dl\.acm|springer|sciencedirect|openreview|proceedings", re.I)),
]


def classify(url):
    for name, pattern in CATEGORIES:
        if pattern.search(url):
            return name
    return "outro"


def clean_url(url):
    url = url.rstrip(".,;:")
    return url


def references_page(doc):
    """Primeira pagina (1-based) onde comeca a secao de referencias."""
    for index, page in enumerate(doc):
        if REFERENCES_RE.search(page.get_text("text")):
            return index + 1
    return None


def cmd_text(args):
    pdf = Path(args.pdf)
    doc = fitz.open(pdf)
    out = Path(args.saida)
    out.parent.mkdir(parents=True, exist_ok=True)
    refs = references_page(doc)
    sparse, tables, figures = [], [], []
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        for index, page in enumerate(doc):
            number = index + 1
            text = page.get_text("text", sort=True)
            handle.write(f"\n=== Página {number} ===\n")
            handle.write(text)
            if len(text.strip()) < 300:
                sparse.append(number)
            if TABLE_RE.search(text):
                tables.append(number)
            if FIGURE_RE.search(text):
                figures.append(number)
    meta = doc.metadata or {}
    print(f"Páginas: {doc.page_count} | texto salvo em {out.as_posix()}")
    if meta.get("title"):
        print(f"Título nos metadados do PDF: {meta['title']}")
    print(f"Referências começam na página: {refs if refs else 'não identificado'}")
    print(f"Páginas com tabela: {tables or '-'} | com figura: {figures or '-'}")
    if sparse:
        print(f"Páginas com pouco texto (podem ser imagem ou escaneadas — ver como imagem se forem relevantes): {sparse}")
    if doc.page_count and len(sparse) == doc.page_count:
        print("ATENÇÃO: nenhuma página tem texto extraível. O PDF parece escaneado; use 'imagem' página a página.")


def cmd_links(args):
    doc = fitz.open(args.pdf)
    refs = references_page(doc)
    found = {}
    for index, page in enumerate(doc):
        number = index + 1
        urls = [link.get("uri") for link in page.get_links() if link.get("uri")]
        text = page.get_text("text")
        joined = re.sub(r"-\n(?=\S)", "", text)          # url quebrada no fim da linha
        urls += URL_RE.findall(joined)
        for url in urls:
            url = clean_url(url)
            if url not in found:
                found[url] = number
    if not found:
        print("Nenhum link encontrado no PDF.")
        return
    order = {name: i for i, (name, _) in enumerate(CATEGORIES)}
    rows = sorted(found.items(), key=lambda kv: (order.get(classify(kv[0]), 99), kv[1]))
    for url, number in rows:
        where = "referências" if refs and number >= refs else f"p. {number}"
        print(f"{classify(url):15} {where:12} {url}")
    print("\nPara QA5/QA6: confira os links de 'codigo' e 'hugging_face' com "
          "'py verificar_link.py <url>'. Links em 'referências' em geral são de outros trabalhos.")


def cmd_image(args):
    doc = fitz.open(args.pdf)
    number = args.pagina
    if not (1 <= number <= doc.page_count):
        sys.exit(f"ERRO: página {number} fora de 1..{doc.page_count}.")
    page = doc[number - 1]
    clip = None
    if args.recorte:
        try:
            x0, y0, x1, y1 = [float(v) for v in args.recorte.split(",")]
        except ValueError:
            sys.exit("ERRO: --recorte deve ser x0,y0,x1,y1 com frações de 0 a 1.")
        r = page.rect
        clip = fitz.Rect(r.x0 + x0 * r.width, r.y0 + y0 * r.height, r.x0 + x1 * r.width, r.y0 + y1 * r.height)
    pix = page.get_pixmap(matrix=fitz.Matrix(args.zoom, args.zoom), clip=clip)
    out = Path(args.saida)
    out.parent.mkdir(parents=True, exist_ok=True)
    pix.save(out)
    print(f"Imagem salva: {out.as_posix()} ({pix.width}x{pix.height}). Abra com a ferramenta Read.")


def main():
    parser = argparse.ArgumentParser(description="Leitura de PDF para o agente.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("texto")
    p.add_argument("pdf")
    p.add_argument("saida")
    p.set_defaults(func=cmd_text)
    p = sub.add_parser("links")
    p.add_argument("pdf")
    p.set_defaults(func=cmd_links)
    p = sub.add_parser("imagem")
    p.add_argument("pdf")
    p.add_argument("pagina", type=int)
    p.add_argument("saida")
    p.add_argument("--zoom", type=float, default=2.0)
    p.add_argument("--recorte")
    p.set_defaults(func=cmd_image)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
