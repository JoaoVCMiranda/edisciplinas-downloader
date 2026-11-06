"""Download incremental dos materiais de cada disciplina do courses.yaml."""
import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from . import api, cursos

VIDEO_EXT = {".mp4", ".mov", ".wmv", ".mkv", ".avi", ".webm", ".m4v"}


def slugify(text):
    t = unicodedata.normalize("NFD", text.strip())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r'[\\/:"*?<>|\x00]', "", t)
    t = re.sub(r"\s+", "-", t)
    t = re.sub(r"-+", "-", t)
    return t.strip("-")


def build_filename(course_id, section, title, ext):
    """<ID>_<seção>_<título>.ext, cabendo no limite de 255 bytes por nome de arquivo."""
    stem = re.sub(r"\.[^./]+$", "", title)
    parts = [p for p in (slugify(course_id), slugify(section), slugify(stem)) if p]
    ext = ext if ext.startswith(".") or not ext else "." + ext
    stem_name = "_".join(parts)
    if len((stem_name + ext).encode()) > 240:  # folga para o ".part" do download
        h = hashlib.sha1(stem_name.encode()).hexdigest()[:6]
        stem_name = stem_name.encode()[: 240 - len(ext.encode()) - 7].decode(errors="ignore").rstrip("-_") + "-" + h
    return stem_name + ext


def parse_courses(text):
    """Parser mínimo do courses.yaml (lista plana de chave: valor) — sem depender de PyYAML."""
    courses, cur = [], None
    for line in text.splitlines():
        line = line.split(" #")[0].rstrip() if not line.lstrip().startswith("#") else ""
        m = re.match(r"^\s*(-\s+)?(id|url|destination|status|sincronizar):\s*(.*)$", line)
        if not m:
            continue
        if m.group(1):
            cur = {}
            courses.append(cur)
        if cur is None:
            raise ValueError("courses.yaml inválido: chave fora de uma entrada '- id: ...'")
        cur[m.group(2)] = m.group(3).strip().strip("\"'")
    for c in courses:
        if not all(c.get(k) for k in ("id", "url", "destination")):
            raise ValueError(f"disciplina incompleta no courses.yaml (precisa de id, url e destination): {c}")
    return courses


def plan_items(course_id, contents):
    """core_course_get_contents -> lista de arquivos.

    Cada item: key, name (nome no disco), section, title, filename, murl, modname, video, fileurl, timemodified, filesize.
    """
    items = []
    for sec in contents:
        for mod in sec.get("modules", []):
            if mod.get("modname") not in ("resource", "folder"):
                continue
            files = [f for f in mod.get("contents", []) if f.get("type") == "file"]
            for f in files:
                ext = os.path.splitext(f["filename"])[1]
                if mod["modname"] == "folder":
                    title = f"{mod['name']}-Pasta_{f['filename']}"
                elif len(files) > 1:  # recurso com vários arquivos: desambigua pelo nome do arquivo
                    title = f"{mod['name']}-Arquivo_{f['filename']}"
                else:
                    title = mod["name"] + "-Arquivo"
                items.append({
                    "key": f"{mod['id']}:{f.get('filepath', '/')}{f['filename']}",
                    "name": build_filename(course_id, sec["name"], title, ext),
                    "section": sec["name"],
                    "title": mod["name"],
                    "filename": f["filename"],
                    "murl": mod.get("url"),
                    "modname": mod["modname"],
                    "_alt": (build_filename(course_id, sec["name"], f"{title}_{f['filename']}", ext),
                             build_filename(course_id, sec["name"], f"{title}_{mod['id']}_{f['filename']}", ext)),
                    "video": ext.lower() in VIDEO_EXT,
                    "fileurl": f["fileurl"],
                    "timemodified": f["timemodified"],
                    "filesize": f["filesize"],
                })
    # recursos diferentes com o mesmo nome na mesma seção (ex.: vários "Slides da aula"): sem isto,
    # um sobrescreveria o outro. Só os que colidem ganham o nome do arquivo (e, se ainda colidir, o id).
    for nivel in (0, 1):
        cont = {}
        for it in items:
            cont[it["name"]] = cont.get(it["name"], 0) + 1
        for it in items:
            if cont[it["name"]] > 1:
                it["name"] = it["_alt"][nivel]
    for it in items:
        del it["_alt"]
    return items


def home():
    try:
        return Path.home()
    except RuntimeError:  # sem $HOME nem entrada no passwd (container)
        return Path("/tmp")


def til(path):
    """Caminho para mostrar: ~/... quando estiver dentro do home."""
    s, h = str(path), str(home())
    return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s


def _mb(n):
    return f"{n / 1048576:.0f} MB"


def sync_course(moodle, course, base, state_dir, log=print, videos=False, dryrun=False):
    """Baixa o que é novo ou mudou. Devolve (sem_falhas, n_baixados)."""
    cid = course["id"]
    dest = Path(base).expanduser() / Path(course["destination"]).expanduser()
    contents = moodle.call("core_course_get_contents", courseid=cursos.mid_da_url(course["url"]))
    items = plan_items(cid, contents)
    pulados = [i for i in items if i["video"] and not videos]
    if pulados:
        items = [i for i in items if i not in pulados]
    if dryrun:
        log(f"{cid}: {len(items)} arquivo(s), {_mb(sum(i['filesize'] for i in items))} -> {til(dest)}"
            + (f" (+ {len(pulados)} vídeo(s), {_mb(sum(i['filesize'] for i in pulados))}, ignorados sem --videos)" if pulados else ""))
        return True, 0
    dest.mkdir(parents=True, exist_ok=True)
    state_path = Path(state_dir) / f"{cid}.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"items": {}}
    falhas = baixados = 0
    for it in items:
        target = dest / it["name"]
        prev = state["items"].get(it["key"])
        if prev and prev.get("indisponivel") and prev["timemodified"] == it["timemodified"]:
            continue  # o servidor recusou (403/404/410) e o arquivo não mudou: não insiste a cada rodada
        if target.exists() and (
            (prev and prev["timemodified"] == it["timemodified"])
            or (not prev and target.stat().st_size == it["filesize"])  # já estava na pasta: adota sem rebaixar
        ):
            state["items"][it["key"]] = {"timemodified": it["timemodified"], "filesize": it["filesize"], "file": it["name"]}
            continue
        tmp = target.with_name(target.name + ".part")
        try:
            moodle.baixar(it["fileurl"], str(tmp))
            os.replace(tmp, target)
        except urllib.error.HTTPError as e:
            tmp.unlink(missing_ok=True)
            if e.code in (403, 404, 410):
                log(f"{cid}: indisponível no servidor (HTTP {e.code}): {it['name']}")
                state["items"][it["key"]] = {"timemodified": it["timemodified"], "filesize": it["filesize"],
                                             "file": it["name"], "indisponivel": True}
            else:
                falhas += 1
                log(f"{cid}: falhou {it['name']}: HTTP {e.code}")
            continue
        except api.LoginError:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as e:
            tmp.unlink(missing_ok=True)
            falhas += 1
            log(f"{cid}: falhou {it['name']}: {type(e).__name__}")
            continue
        baixados += 1
        log(f"{cid}: baixado {it['name']}")
        state["items"][it["key"]] = {"timemodified": it["timemodified"], "filesize": it["filesize"], "file": it["name"]}
    state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    os.replace(tmp, state_path)
    extra = f", {len(pulados)} vídeo(s) ignorado(s) (use --videos)" if pulados else ""
    log(f"{cid}: {len(items) - falhas}/{len(items)} ok, {baixados} novo(s)/alterado(s){extra}")
    return falhas == 0, baixados


def status_efetivo(course, matr):
    """status do courses.yaml; se ausente, pelo fim da disciplina no Moodle."""
    c = matr.get(cursos.mid_da_url(course["url"]))
    return course.get("status") or (cursos.status(c) if c else "em_curso")


def a_sincronizar(course, matr, state_dir, pedidos, now=None):
    """Disciplina concluída (ou sincronizar: semanal) só é conferida uma vez por semana, salvo pedido explícito."""
    if pedidos:
        return True
    sinc = course.get("sincronizar") or ("semanal" if status_efetivo(course, matr) == "concluida" else None)
    if sinc != "semanal":
        return True
    try:
        st = json.loads((Path(state_dir) / f"{course['id']}.json").read_text())
        ts = datetime.fromisoformat(st["updated_at"]).timestamp()
    except Exception:
        return True
    return (now or time.time()) - ts >= 7 * 86400 - 3600
