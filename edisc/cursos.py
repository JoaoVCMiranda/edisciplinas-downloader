"""Matrículas do Moodle -> entradas do courses.yaml."""
import re
import time
from datetime import datetime

from .auth import SITE

_COD = re.compile(r"(?<![A-Za-z0-9])([A-Z]{3}\d{4}|\d{7})(?![A-Za-z0-9])")


def codigo(c):
    """Código da disciplina (Júpiter): idnumber ("ABC1234.1.2026201,...") > fullname > shortname; None se não achar."""
    for fonte in ((c.get("idnumber") or "").split(",")[0].split(".")[0], c.get("fullname") or "", c.get("shortname") or ""):
        m = _COD.search(fonte)
        if m:
            return m.group(1)
    return None


def periodo(startdate):
    d = datetime.fromtimestamp(startdate)
    return d.year, 1 if d.month < 7 else 2


def status(c, now=None):
    end = c.get("enddate") or 0
    return "concluida" if end and end < (now or time.time()) else "em_curso"


def matriculas(moodle):
    """{id_moodle: curso} das disciplinas em que o usuário está matriculado (cache por execução)."""
    if getattr(moodle, "_matriculas", None) is None:
        uid = moodle.call("core_webservice_get_site_info")["userid"]
        moodle._matriculas = {c["id"]: c for c in moodle.call("core_enrol_get_users_courses", userid=uid)}
    return moodle._matriculas


def mid_da_url(url):
    return int(re.search(r"id=(\d+)", url).group(1))


def montar_entradas(matr, existentes):
    """Entradas novas para o courses.yaml (as existentes ficam como estão).

    Pasta padrão: <ANO>-<SEM>/<CÓDIGO>, relativa à pasta de downloads.
    """
    ja = {mid_da_url(e["url"]) for e in existentes}
    usados = {e["destination"] for e in existentes}
    novas, avisos, cods = [], [], {}
    for c in matr.values():
        cods.setdefault(codigo(c) or c["shortname"], []).append(c)
    for c in sorted(matr.values(), key=lambda c: -c["startdate"]):
        if c["id"] in ja:
            continue
        cod = codigo(c)
        if not cod:
            cod = re.sub(r"[^A-Za-z0-9]+", "-", c["shortname"]).strip("-") or str(c["id"])
            avisos.append(f"{c['id']} {c['shortname']!r}: código não reconhecido, usando {cod!r}")
        ano, sem = periodo(c["startdate"])
        repetido = len(cods.get(codigo(c) or c["shortname"], [])) > 1
        cid = f"{cod}-{ano}-{sem}" if repetido else cod
        dest = f"{ano}-{sem}/{cod}"
        if dest in usados:
            dest += f"-{c['id']}"
            cid += f"-{c['id']}"
            avisos.append(f"{c['id']}: pasta repetida no mesmo semestre, sufixo -{c['id']}")
        usados.add(dest)
        e = {"id": cid, "url": f"{SITE}/course/view.php?id={c['id']}", "destination": dest, "status": status(c)}
        if e["status"] == "concluida":
            e["sincronizar"] = "semanal"
        novas.append((c, e))
    return novas, avisos


CABECALHO = """\
# Disciplinas que o edisc baixa. Gerado por `edisc cursos --gerar`; pode editar à vontade.
#
#   id           nome curto, usado como prefixo dos arquivos
#   url          página da disciplina no e-Disciplinas
#   destination  pasta de destino (relativa à pasta de downloads, ou caminho absoluto)
#   status       em_curso | concluida (opcional)
#   sincronizar  semanal = só confere uma vez por semana (padrão para concluídas)
#
# Para parar de baixar uma disciplina, apague (ou comente) a entrada dela.
"""


def yaml_cursos(matr, existentes):
    novas, avisos = montar_entradas(matr, existentes)
    linhas = [CABECALHO, "courses:"]

    def bloco(e, c):
        nome = (c or {}).get("fullname")
        if nome:
            linhas.append(f"  # {nome}")
        linhas.append(f"  - id: {e['id']}")
        linhas.append(f"    url: {e['url']}")
        linhas.append(f"    destination: {e['destination']}")
        for k in ("status", "sincronizar"):
            if e.get(k):
                linhas.append(f"    {k}: {e[k]}")
        linhas.append("")

    for e in existentes:  # existentes primeiro, intactas (status preenchido só se faltava)
        c = matr.get(mid_da_url(e["url"]))
        e = dict(e)
        if c and not e.get("status"):
            e["status"] = status(c)
        bloco(e, c)
    for c, e in novas:
        bloco(e, c)
    return "\n".join(linhas), avisos
