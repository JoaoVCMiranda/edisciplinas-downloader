"""edisc — baixa os materiais das suas disciplinas do e-Disciplinas (USP)."""
import argparse
import getpass
import os
import shutil
import sys
from pathlib import Path

from . import __version__, api, auth, cursos, sync


def caminhos():
    env = os.environ.get
    xdg_cfg = Path(env("XDG_CONFIG_HOME") or sync.home() / ".config")
    xdg_state = Path(env("XDG_STATE_HOME") or sync.home() / ".local/state")
    cfg = Path(env("EDISC_CONFIG") or xdg_cfg / "edisciplinas")
    return {
        "config": cfg,
        "courses": Path(env("EDISC_COURSES") or cfg / "courses.yaml"),
        "token": cfg / "token",
        "credenciais": cfg / "credenciais",
        "estado": Path(env("EDISC_STATE") or xdg_state / "edisciplinas"),
        "pasta": Path(env("EDISC_DIR") or sync.home() / "edisciplinas"),
    }


def _store(p):
    return api.TokenStore(str(p["token"]), str(p["credenciais"]))


def _ler_courses(p, obrigatorio=True):
    try:
        return sync.parse_courses(p["courses"].read_text(encoding="utf-8"))
    except FileNotFoundError:
        if obrigatorio:
            raise SystemExit(f"Não achei {sync.til(p['courses'])}. Rode `edisc cursos --gerar` primeiro.")
        return []


def cmd_login(a, p):
    if sys.stdin.isatty():
        user = input("Número USP: ").strip()
        senha = getpass.getpass("Senha única: ")
    else:  # não interativo: duas linhas no stdin (usuário, senha)
        linhas = sys.stdin.read().splitlines()
        if len(linhas) < 2:
            raise SystemExit("Sem terminal: mande usuário e senha em duas linhas pelo stdin.")
        user, senha = linhas[0].strip(), linhas[1]
    print("Entrando pelo SSO da USP...", file=sys.stderr)
    try:
        tok = auth.fetch_token(user, senha)
    except Exception as e:
        print(f"Login falhou: {e}", file=sys.stderr)
        return 2
    _store(p).save(tok)
    if a.lembrar:
        api.write_private(str(p["credenciais"]), f"user: {user}\npass: {senha}\n")
        print(f"Credenciais guardadas em {sync.til(p['credenciais'])} (0600) para renovar o token sozinho.", file=sys.stderr)
    elif p["credenciais"].exists():
        p["credenciais"].unlink()
        print("Credenciais antigas apagadas (use --lembrar para guardar).", file=sys.stderr)
    print(f"Ok. Token salvo em {sync.til(p['token'])}.", file=sys.stderr)
    return 0


def cmd_cursos(a, p):
    moodle = api.Moodle(_store(p))
    matr = cursos.matriculas(moodle)
    existentes = _ler_courses(p, obrigatorio=False)
    if a.gerar:
        texto, avisos = cursos.yaml_cursos(matr, existentes)
        for av in avisos:
            print(f"aviso: {av}", file=sys.stderr)
        p["courses"].parent.mkdir(parents=True, exist_ok=True)
        if p["courses"].exists():
            shutil.copy2(p["courses"], p["courses"].with_suffix(".yaml.bak"))
        p["courses"].write_text(texto + "\n", encoding="utf-8")
        n = len(sync.parse_courses(texto))
        print(f"{n} disciplina(s) em {sync.til(p['courses'])} ({n - len(existentes)} nova(s)). "
              "Revise o arquivo e apague o que não quiser baixar.", file=sys.stderr)
        return 0
    ja = {cursos.mid_da_url(e["url"]) for e in existentes}
    print("id_moodle\tsemestre\tstatus\tcódigo\tcourses.yaml\tnome")
    for c in sorted(matr.values(), key=lambda c: -c["startdate"]):
        ano, sem = cursos.periodo(c["startdate"])
        print(f"{c['id']}\t{ano}-{sem}\t{cursos.status(c)}\t{cursos.codigo(c) or '?'}\t"
              f"{'sim' if c['id'] in ja else 'não'}\t{c['fullname'][:60]}")
    return 0


def cmd_sync(a, p):
    base = Path(a.pasta).expanduser() if a.pasta else p["pasta"]
    courses = _ler_courses(p)
    if a.ids:
        desconhecidos = set(a.ids) - {c["id"] for c in courses}
        if desconhecidos:
            raise SystemExit(f"id(s) fora do courses.yaml: {', '.join(sorted(desconhecidos))}")
        courses = [c for c in courses if c["id"] in a.ids]
    moodle = api.Moodle(_store(p))
    try:
        matr = cursos.matriculas(moodle)
    except api.LoginError:
        raise
    except Exception:
        matr = {}
    courses = [c for c in courses if sync.a_sincronizar(c, matr, p["estado"], a.ids)]
    print(f"{len(courses)} disciplina(s) para conferir -> {sync.til(base)}")
    erros, total = [], 0
    for c in courses:
        try:
            ok, n = sync.sync_course(moodle, c, base, p["estado"], videos=a.videos, dryrun=a.dryrun)
            total += n
            if not ok:
                erros.append(c["id"])
        except api.LoginError:
            raise
        except Exception as e:
            erros.append(c["id"])
            print(f"{c['id']}: erro: {type(e).__name__}: {e}", file=sys.stderr)
    if erros:
        print(f"Terminou com erro em: {', '.join(erros)}", file=sys.stderr)
        return 1
    if not a.dryrun:
        print(f"Pronto: {total} arquivo(s) novo(s)/alterado(s).")
    return 0


def parser():
    p = caminhos()
    ap = argparse.ArgumentParser(
        prog="edisc",
        description="Baixa os materiais das suas disciplinas do e-Disciplinas (USP).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "primeiro uso:\n"
            "  edisc login --lembrar   # Número USP + senha única\n"
            "  edisc cursos --gerar    # cria o courses.yaml com as suas matrículas\n"
            "  edisc sync              # baixa tudo\n\n"
            f"config:    {sync.til(p['courses'])}\n"
            f"downloads: {sync.til(p['pasta'])}  (EDISC_DIR)\n"
            f"estado:    {sync.til(p['estado'])}"
        ),
    )
    ap.add_argument("--version", action="version", version=f"edisc {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="comando")

    lg = sub.add_parser("login", help="entra pelo SSO da USP e guarda o token")
    lg.add_argument("--lembrar", action="store_true",
                    help="guarda usuário e senha (arquivo 0600) para renovar o token sem você; bom para cron")
    lg.set_defaults(fn=cmd_login)

    cs = sub.add_parser("cursos", help="lista suas matrículas; --gerar cria/atualiza o courses.yaml")
    cs.add_argument("--gerar", action="store_true", help="acrescenta as disciplinas que faltam no courses.yaml")
    cs.set_defaults(fn=cmd_cursos)

    sy = sub.add_parser("sync", help="baixa o que é novo ou mudou")
    sy.add_argument("ids", nargs="*", help="só estas disciplinas (id do courses.yaml)")
    sy.add_argument("--videos", action="store_true", help="baixa também os vídeos (podem ser grandes)")
    sy.add_argument("--dryrun", "--dry-run", dest="dryrun", action="store_true", help="só mostra quanto seria baixado")
    sy.add_argument("--pasta", help="pasta de downloads (padrão: EDISC_DIR ou ~/edisciplinas)")
    sy.set_defaults(fn=cmd_sync)
    return ap


def main(argv=None):
    ap = parser()
    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 0
    try:
        return a.fn(a, caminhos())
    except api.LoginError as e:
        print(f"Login: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except (ValueError, api.ApiError) as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1
