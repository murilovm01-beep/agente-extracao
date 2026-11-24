#!/usr/bin/env python3
"""
verificar_link.py - confere se links de codigo e artefatos abrem (apoio a QA5/QA6).

USO (Windows, na pasta do projeto):
    py verificar_link.py https://github.com/autor/repositorio [outros links...]

Para cada link imprime uma linha:
    ok              repositorio GitHub com arquivos alem de README/LICENSE,
                    ou pagina do Hugging Face que abre
    somente_readme  repositorio existe mas so tem README/LICENSE ("coming soon")
    vazio           repositorio existe e esta vazio
    nao_encontrado  404
    manual          Google Drive, SharePoint, OneDrive, Baidu, Dropbox e afins:
                    o acesso automatizado nao prova que o arquivo esta disponivel
    erro            falha de rede, limite da API ou resposta inesperada

Usa o curl.exe (vem no Windows 10/11): na maquina do projeto, o cliente HTTP do
Python foi bloqueado em outros sites e o curl passou. Se o curl nao existir,
tenta o urllib.

A API publica do GitHub permite 60 consultas por hora sem login.
"""

import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

USER_AGENT = "mapeamento-sistematico-verificador/1.0"
MANUAL_HOSTS = re.compile(r"drive\.google|docs\.google|sharepoint|onedrive|1drv\.ms|dropbox|pan\.baidu|"
                          r"box\.com|mega\.nz|weiyun|aliyundrive|quark", re.I)
TRIVIAL_FILES = {"readme", "readme.md", "readme.rst", "readme.txt", "license", "license.md",
                 "license.txt", ".gitignore", ".gitattributes", "assets", "docs", "images", "figs", "teaser.png"}


def http_get(url, accept="application/json", head_only=False):
    """Devolve (status, corpo_em_texto). status 0 = falha de rede."""
    curl = shutil.which("curl")
    if curl:
        cmd = [curl, "-sS", "-L", "--max-time", "30", "-A", USER_AGENT, "-H", f"Accept: {accept}",
               "-w", "\n__STATUS__%{http_code}"]
        if head_only:
            cmd += ["-o", "NUL" if sys.platform.startswith("win") else "/dev/null"]
        cmd.append(url)
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=45)
            text = out.stdout.decode("utf-8", errors="replace")
            body, _, code = text.rpartition("__STATUS__")
            return (int(code) if code.strip().isdigit() else 0), body
        except (subprocess.SubprocessError, OSError):
            pass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept},
                                     method="HEAD" if head_only else "GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, "" if head_only else resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        return err.code, ""
    except (urllib.error.URLError, OSError):
        return 0, ""


def check_github(owner, repo):
    repo = re.sub(r"\.git$", "", repo)
    status, body = http_get(f"https://api.github.com/repos/{owner}/{repo}")
    if status == 404:
        return "nao_encontrado", "repositório não existe ou é privado"
    if status in (403, 429):
        if "rate limit" in body.lower():
            return "erro", "limite da API do GitHub atingido (60/h); tente mais tarde ou confira à mão"
        return "erro", f"acesso negado (HTTP {status}); confira à mão"
    if status != 200:
        return "erro", f"HTTP {status}"
    try:
        info = json.loads(body)
    except json.JSONDecodeError:
        return "erro", "resposta inesperada da API"
    detail = f"atualizado em {str(info.get('pushed_at', ''))[:10]}"
    if info.get("archived"):
        detail += ", arquivado"
    if info.get("size", 0) == 0:
        return "vazio", detail
    status, body = http_get(f"https://api.github.com/repos/{owner}/{repo}/contents")
    if status != 200:
        return "ok", detail + " (não consegui listar os arquivos)"
    try:
        names = [item.get("name", "").lower() for item in json.loads(body)]
    except (json.JSONDecodeError, AttributeError):
        return "ok", detail
    substantive = [n for n in names if n not in TRIVIAL_FILES]
    if not substantive:
        return "somente_readme", detail + f"; arquivos: {', '.join(names) or 'nenhum'}"
    return "ok", detail + f"; {len(names)} itens na raiz"


def check(url):
    url = url.strip().rstrip(".,;:)")
    gh = re.match(r"https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+)", url, re.I)
    if gh and gh.group(1).lower() not in ("orgs", "topics", "sponsors", "features"):
        return check_github(gh.group(1), gh.group(2))
    if MANUAL_HOSTS.search(url):
        return "manual", "armazenamento em nuvem: conferir à mão se o arquivo abre e é o certo"
    status, _ = http_get(url, accept="text/html,*/*", head_only=True)
    if status == 404:
        return "nao_encontrado", "HTTP 404"
    if 200 <= status < 400:
        if re.search(r"huggingface\.co/", url, re.I):
            return "ok", f"HTTP {status}"
        return "manual", f"HTTP {status}: a página abre, mas confira se o artefato está lá"
    return "erro", f"HTTP {status}" if status else "falha de rede"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for url in sys.argv[1:]:
        status, detail = check(url)
        print(f"{status:15} {url} — {detail}")


if __name__ == "__main__":
    main()
