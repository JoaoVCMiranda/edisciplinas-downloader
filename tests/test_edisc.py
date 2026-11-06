import base64
import http.server
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from edisc import api, auth, cli, cursos, sync


def contents(tm=1, video=False):
    f = lambda n, s=10: {"type": "file", "filename": n, "fileurl": f"https://x/{n}", "timemodified": tm, "filesize": s}
    mods = [
        {"id": 1, "modname": "resource", "name": "Aula 1", "contents": [f("a.pdf")]},
        {"id": 2, "modname": "forum", "name": "Avisos"},
    ]
    if video:
        mods.append({"id": 4, "modname": "resource", "name": "Gravação", "contents": [f("g.mp4", 999)]})
    return [
        {"name": "Material das aulas", "modules": mods},
        {"name": "Listas", "modules": [
            {"id": 3, "modname": "folder", "name": "Exercícios", "contents": [f("lista1.pdf"), f("lista2.pdf")]},
        ]},
    ]


class Fake:
    def __init__(self, tm=1, video=False, falha=None):
        self.tm, self.video, self.falha, self.calls = tm, video, falha, []

    def call(self, fn, **kw):
        return contents(self.tm, self.video)

    def baixar(self, url, tmp):
        self.calls.append(url)
        if self.falha and self.falha in url:
            raise self.falha_exc
        Path(tmp).write_bytes(b"x" * 10)
        return 10


COURSE = {"id": "ABC1234", "url": "https://e/course/view.php?id=7", "destination": "2026-2/ABC1234"}
quiet = lambda *_: None


class TNomes(unittest.TestCase):
    def test_nomes(self):
        names = [i["name"] for i in sync.plan_items("ABC1234", contents())]
        self.assertEqual(names, ["ABC1234_Material-das-aulas_Aula-1-Arquivo.pdf",
                                 "ABC1234_Listas_Exercicios-Pasta_lista1.pdf",
                                 "ABC1234_Listas_Exercicios-Pasta_lista2.pdf"])

    def test_mesmo_nome_na_mesma_secao_nao_sobrescreve(self):
        f = lambda n: {"type": "file", "filename": n, "fileurl": f"https://x/{n}", "timemodified": 1, "filesize": 1}
        sec = [{"name": "S", "modules": [
            {"id": 10, "modname": "resource", "name": "Slides", "contents": [f("aula1.pdf")]},
            {"id": 11, "modname": "resource", "name": "Slides", "contents": [f("aula2.pdf")]},
            {"id": 12, "modname": "resource", "name": "Slides", "contents": [f("aula2.pdf")]},
            {"id": 13, "modname": "resource", "name": "Outro", "contents": [f("x.pdf")]},
        ]}]
        names = [i["name"] for i in sync.plan_items("X", sec)]
        self.assertEqual(len(set(names)), 4)
        self.assertEqual(names[0], "X_S_Slides-Arquivo_aula1.pdf")
        self.assertEqual(names[1:3], ["X_S_Slides-Arquivo_11_aula2.pdf", "X_S_Slides-Arquivo_12_aula2.pdf"])
        self.assertEqual(names[3], "X_S_Outro-Arquivo.pdf")  # quem não colide mantém o nome

    def test_nome_longo_truncado(self):
        n = sync.build_filename("X", "sec", "t" * 400 + ".pdf", ".pdf")
        self.assertLessEqual(len(n.encode()), 240)
        self.assertTrue(n.endswith(".pdf"))


class TConfig(unittest.TestCase):
    def test_courses_yaml(self):
        c = sync.parse_courses("# x\ncourses:\n  - id: A  # comentário\n    url: https://e/course/view.php?id=7\n    destination: d/e\n")
        self.assertEqual(c, [{"id": "A", "url": "https://e/course/view.php?id=7", "destination": "d/e"}])
        self.assertEqual(cursos.mid_da_url(c[0]["url"]), 7)

    def test_courses_incompleto(self):
        with self.assertRaises(ValueError):
            sync.parse_courses("courses:\n  - id: A\n")

    def test_credenciais_com_aspas(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "c"
            f.write_text("user: 123\npass: abc'\n")
            self.assertEqual(auth.read_credentials(f), ("123", "abc'"))
            f.write_text("user: '123'\npass: \"x y\"\n")
            self.assertEqual(auth.read_credentials(f), ("123", "x y"))

    def test_token_url(self):
        b = base64.b64encode(b"site:::TOKEN123:::priv").decode()
        self.assertEqual(auth.parse_token_url(f"moodlemobile://token={b}"), "TOKEN123")


class TSync(unittest.TestCase):
    def setUp(self):
        self.base, self.state = tempfile.mkdtemp(), tempfile.mkdtemp()

    def test_incremental(self):
        m = Fake(1)
        self.assertEqual(sync.sync_course(m, COURSE, self.base, self.state, quiet), (True, 3))
        m2 = Fake(1)
        sync.sync_course(m2, COURSE, self.base, self.state, quiet)
        self.assertEqual(m2.calls, [])
        m3 = Fake(2)
        self.assertEqual(sync.sync_course(m3, COURSE, self.base, self.state, quiet), (True, 3))
        self.assertTrue((Path(self.base) / "2026-2/ABC1234/ABC1234_Listas_Exercicios-Pasta_lista1.pdf").exists())

    def test_rebaixa_arquivo_apagado(self):
        sync.sync_course(Fake(1), COURSE, self.base, self.state, quiet)
        (Path(self.base) / "2026-2/ABC1234/ABC1234_Material-das-aulas_Aula-1-Arquivo.pdf").unlink()
        m = Fake(1)
        sync.sync_course(m, COURSE, self.base, self.state, quiet)
        self.assertEqual(m.calls, ["https://x/a.pdf"])

    def test_adota_arquivo_existente_pelo_tamanho(self):
        d = Path(self.base) / "2026-2/ABC1234"
        d.mkdir(parents=True)
        (d / "ABC1234_Material-das-aulas_Aula-1-Arquivo.pdf").write_bytes(b"x" * 10)
        m = Fake(1)
        sync.sync_course(m, COURSE, self.base, self.state, quiet)
        self.assertEqual(len(m.calls), 2)

    def test_videos_so_com_flag(self):
        m = Fake(1, video=True)
        sync.sync_course(m, COURSE, self.base, self.state, quiet)
        self.assertNotIn("https://x/g.mp4", m.calls)
        m2 = Fake(1, video=True)
        sync.sync_course(m2, COURSE, self.base, self.state, quiet, videos=True)
        self.assertEqual(m2.calls, ["https://x/g.mp4"])

    def test_dryrun_nao_baixa(self):
        m = Fake(1, video=True)
        out = []
        self.assertEqual(sync.sync_course(m, COURSE, self.base, self.state, out.append, dryrun=True), (True, 0))
        self.assertEqual(m.calls, [])
        self.assertIn("1 vídeo(s)", out[0])

    def test_404_marca_indisponivel_e_nao_insiste(self):
        m = Fake(1, falha="a.pdf")
        m.falha_exc = urllib.error.HTTPError("u", 404, "x", {}, None)
        self.assertEqual(sync.sync_course(m, COURSE, self.base, self.state, quiet), (True, 2))
        m2 = Fake(1)
        sync.sync_course(m2, COURSE, self.base, self.state, quiet)
        self.assertEqual(m2.calls, [])

    def test_erro_transitorio_falha_e_tenta_de_novo(self):
        m = Fake(1, falha="a.pdf")
        m.falha_exc = TimeoutError()
        self.assertEqual(sync.sync_course(m, COURSE, self.base, self.state, quiet), (False, 2))
        m2 = Fake(1)
        sync.sync_course(m2, COURSE, self.base, self.state, quiet)
        self.assertEqual(m2.calls, ["https://x/a.pdf"])

    def test_semanal(self):
        c = dict(COURSE, status="concluida")
        self.assertTrue(sync.a_sincronizar(c, {}, self.state, []))
        Path(self.state, "ABC1234.json").write_text(json.dumps({"updated_at": datetime.now(timezone.utc).isoformat()}))
        self.assertFalse(sync.a_sincronizar(c, {}, self.state, []))
        self.assertTrue(sync.a_sincronizar(c, {}, self.state, ["ABC1234"]))
        self.assertTrue(sync.a_sincronizar(c, {}, self.state, [], now=time.time() + 8 * 86400))
        self.assertTrue(sync.a_sincronizar(dict(c, status="em_curso"), {}, self.state, []))


def curso(i, short, full, idn, start, end=1):
    return {"id": i, "shortname": short, "fullname": full, "idnumber": idn, "startdate": start, "enddate": end}


class TCursos(unittest.TestCase):
    def test_codigo(self):
        self.assertEqual(cursos.codigo(curso(1, "x", "x", "ABC1234.1.2026201,ABC1234.1.2026202", 0)), "ABC1234")
        self.assertEqual(cursos.codigo(curso(1, "1234567-2024", "1234567 - Física", "", 0)), "1234567")
        self.assertEqual(cursos.codigo(curso(1, "2023-1-x", "XYZ0001 - Cálculo", "", 0)), "XYZ0001")
        self.assertIsNone(cursos.codigo(curso(1, "foo", "Bar", "", 0)))

    def test_periodo_e_status(self):
        self.assertEqual(cursos.periodo(time.mktime((2026, 5, 5, 0, 0, 0, 0, 0, -1))), (2026, 1))
        self.assertEqual(cursos.periodo(time.mktime((2026, 8, 3, 0, 0, 0, 0, 0, -1))), (2026, 2))
        self.assertEqual(cursos.status({"enddate": 100}, 200), "concluida")
        self.assertEqual(cursos.status({"enddate": 300}, 200), "em_curso")

    def test_gerar_mantem_existentes_e_separa_repetidos(self):
        s1 = time.mktime((2024, 3, 1, 0, 0, 0, 0, 0, -1))
        s2 = time.mktime((2024, 8, 5, 0, 0, 0, 0, 0, -1))
        m = {1: curso(1, "ABC1111-1", "ABC1111 - Lab", "ABC1111.3.1", s1), 2: curso(2, "ABC1111-2", "ABC1111 - Lab", "ABC1111.4.1", s2),
             3: curso(3, "XYZ2222", "XYZ2222 - A", "XYZ2222.1.1", s2)}
        ex = [{"id": "XYZ2222", "url": "https://e/course/view.php?id=3", "destination": "minha/pasta"}]
        novas, _ = cursos.montar_entradas(m, ex)
        self.assertEqual({e["id"]: e["destination"] for _, e in novas},
                         {"ABC1111-2024-2": "2024-2/ABC1111", "ABC1111-2024-1": "2024-1/ABC1111"})
        txt, _ = cursos.yaml_cursos(m, ex)
        parsed = sync.parse_courses(txt)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed[0]["destination"], "minha/pasta")
        self.assertIn("sincronizar: semanal", txt)


class TApi(unittest.TestCase):
    def test_token_invalido_renova(self):
        class S:
            def get(s): return "old"
            def renew(s): return "new"
        m = api.Moodle(S())
        seq = [api.InvalidToken(), {"ok": 1}]

        def raw(fn, p):
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        m._raw = raw
        self.assertEqual(m.call("f"), {"ok": 1})
        self.assertEqual(m.token, "new")

    def test_sem_credenciais_pede_login(self):
        with tempfile.TemporaryDirectory() as d:
            st = api.TokenStore(os.path.join(d, "token"), os.path.join(d, "credenciais"))
            with self.assertRaisesRegex(api.LoginError, "edisc login"):
                st.get()
            st.save("T")
            self.assertEqual(st.get(), "T")
            self.assertEqual(os.stat(os.path.join(d, "token")).st_mode & 0o777, 0o600)

    def test_json_legitimo_vs_erro(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(s):
                body = b'{"cells": []}' if "nb" in s.path else b'{"exception":"x","errorcode":"invalidtoken"}'
                s.send_response(200)
                s.send_header("Content-Type", "application/json")
                s.end_headers()
                s.wfile.write(body)

            def log_message(s, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        m = api.Moodle.__new__(api.Moodle)
        m.token = "t"
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.ipynb"
            self.assertEqual(m.download(f"http://127.0.0.1:{srv.server_port}/nb", str(f)), 13)
            with self.assertRaises(api.InvalidToken):
                m.download(f"http://127.0.0.1:{srv.server_port}/err", str(f))
        srv.shutdown()

    def _retry(self, falhas):
        m = api.Moodle.__new__(api.Moodle)
        m.token, m.n = "t", 0
        m.esperas = []
        m.dormir = m.esperas.append

        class S:
            @staticmethod
            def renew(): return "novo"
        m.store = S

        def dl(url, tmp):
            m.n += 1
            if falhas:
                raise falhas.pop(0)
            return 7
        m.download = dl
        return m

    def test_retry(self):
        m = self._retry([TimeoutError(), TimeoutError()])
        self.assertEqual(m.baixar("u", "t"), 7)
        self.assertEqual((m.n, m.esperas), (3, [3, 10]))
        m = self._retry([TimeoutError()] * 5)
        with self.assertRaises(TimeoutError):
            m.baixar("u", "t")
        self.assertEqual(m.n, 3)
        m = self._retry([urllib.error.HTTPError("u", 403, "x", {}, None)])
        with self.assertRaises(urllib.error.HTTPError):
            m.baixar("u", "t")
        self.assertEqual(m.n, 1)
        m = self._retry([api.InvalidToken()])
        self.assertEqual(m.baixar("u", "t"), 7)
        self.assertEqual(m.token, "novo")

    def test_call_repete_queda_de_conexao(self):
        import http.client
        m = api.Moodle.__new__(api.Moodle)
        m.token, esperas = "t", []
        m.dormir = esperas.append
        seq = [http.client.RemoteDisconnected("x"), {"ok": 1}]

        def raw(fn, p):
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        m._raw = raw
        self.assertEqual(m.call("f"), {"ok": 1})
        self.assertEqual(esperas, [3])


class TCli(unittest.TestCase):
    def test_sem_courses_yaml(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"EDISC_CONFIG": d}):
            with self.assertRaisesRegex(SystemExit, "cursos --gerar"):
                cli.main(["sync"])

    def test_help(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main([]), 0)
        self.assertIn("edisc login", out.getvalue())

    def test_sync_ponta_a_ponta(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"EDISC_CONFIG": f"{d}/cfg", "EDISC_STATE": f"{d}/st", "EDISC_DIR": f"{d}/dl"}
            Path(d, "cfg").mkdir()
            Path(d, "cfg/courses.yaml").write_text(
                "courses:\n  - id: ABC1234\n    url: https://e/course/view.php?id=7\n    destination: 2026-2/ABC1234\n")
            fake = Fake(1)
            fake._matriculas = {}
            with mock.patch.dict(os.environ, env), mock.patch.object(api, "Moodle", lambda store: fake), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["sync"]), 0)
            self.assertEqual(len(list(Path(d, "dl/2026-2/ABC1234").iterdir())), 3)


if __name__ == "__main__":
    unittest.main()
