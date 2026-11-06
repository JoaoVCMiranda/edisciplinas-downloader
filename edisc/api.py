"""Cliente mínimo da REST API do Moodle (serviço moodle_mobile_app) — só stdlib."""
import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from . import auth

REST = f"{auth.SITE}/webservice/rest/server.php"


TRANSITORIOS = (TimeoutError, socket.timeout, ConnectionError, http.client.HTTPException, urllib.error.URLError)


def transitorio(e):
    """Vale tentar de novo? HTTPError só em 408/429/5xx; queda de conexão/timeout sempre."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (408, 429) or e.code >= 500
    return isinstance(e, TRANSITORIOS)


class InvalidToken(Exception):
    pass


class ApiError(Exception):
    pass


class LoginError(ApiError):
    """Token inválido e a renovação via SSO falhou (senha trocada, SSO mudou, rede)."""


def write_private(path, text):
    """Grava arquivo com permissão 0600 (token, credenciais)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


class TokenStore:
    """Token em arquivo 0600; renovado via SSO só quando a API devolve invalidtoken.

    Sem arquivo de credenciais (`edisc login` sem --lembrar), a renovação não é possível:
    o erro pede para rodar `edisc login` de novo.
    """

    def __init__(self, token_file, creds_file=None):
        self.token_file, self.creds_file = token_file, creds_file

    def get(self):
        try:
            with open(self.token_file, encoding="utf-8") as f:
                tok = f.read().strip()
        except FileNotFoundError:
            tok = ""
        return tok or self.renew()

    def renew(self):
        if not (self.creds_file and os.path.exists(self.creds_file)):
            raise LoginError("sem token válido: rode `edisc login`")
        try:
            user, password = auth.read_credentials(self.creds_file)
            tok = auth.fetch_token(user, password)
        except Exception as e:
            raise LoginError(f"{type(e).__name__}: {e} (rode `edisc login`)") from None
        self.save(tok)
        return tok

    def save(self, tok):
        write_private(self.token_file, tok)


class Moodle:
    def __init__(self, store):
        self.store = store
        self.token = store.get()

    def _raw(self, fn, params):
        body = urllib.parse.urlencode(
            {"wstoken": self.token, "wsfunction": fn, "moodlewsrestformat": "json", **params}
        ).encode()
        req = urllib.request.Request(REST, body, headers={"User-Agent": "MoodleMobile"})
        data = json.load(urllib.request.urlopen(req, timeout=60))
        if isinstance(data, dict) and data.get("exception"):
            if data.get("errorcode") == "invalidtoken":
                raise InvalidToken()
            raise ApiError(f"{fn}: {data.get('errorcode')}: {data.get('message')}")
        return data

    espera = (3, 10)  # segundos entre tentativas em falha transitória
    dormir = staticmethod(time.sleep)

    def _com_retry(self, f):
        """f() com renovação de token (uma vez) e retry com backoff em falha transitória."""
        renovou = False
        for i in range(len(self.espera) + 1):
            try:
                return f()
            except InvalidToken:
                if renovou:
                    raise LoginError("token recém-renovado também inválido") from None
                renovou = True
                self.token = self.store.renew()
            except TRANSITORIOS as e:
                if not transitorio(e) or i == len(self.espera):
                    raise
                self.dormir(self.espera[i])
        return f()  # última tentativa depois de renovar o token no fim do laço

    def call(self, fn, **params):
        return self._com_retry(lambda: self._raw(fn, params))

    def download(self, fileurl, dest_tmp):
        """Baixa fileurl (+token) para dest_tmp; devolve nº de bytes. Levanta InvalidToken se o servidor devolver JSON de erro."""
        sep = "&" if "?" in fileurl else "?"
        req = urllib.request.Request(
            f"{fileurl}{sep}token={urllib.parse.quote(self.token)}", headers={"User-Agent": "MoodleMobile"}
        )
        with urllib.request.urlopen(req, timeout=120) as r, open(dest_tmp, "wb") as out:
            if "json" in (r.headers.get("Content-Type") or ""):
                # JSON pode ser um arquivo legítimo (.ipynb); só é erro do Moodle se trouxer exception/errorcode
                body = r.read()
                try:
                    err = json.loads(body)
                except ValueError:
                    err = None
                if isinstance(err, dict) and (err.get("exception") or err.get("errorcode")):
                    if err.get("errorcode") == "invalidtoken":
                        raise InvalidToken()
                    raise ApiError(f"download: {err.get('errorcode')}")
                out.write(body)
                return len(body)
            n = 0
            while chunk := r.read(1 << 16):
                out.write(chunk)
                n += len(chunk)
        return n

    def baixar(self, fileurl, dest_tmp):
        """download() com renovação de token e retry (timeout, rede, 5xx/429); 403/404/410 sobem na hora."""
        return self._com_retry(lambda: self.download(fileurl, dest_tmp))
