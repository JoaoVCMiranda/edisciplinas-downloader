"""Token do app Moodle via SSO (Shibboleth/SAML por HTTP puro — sem navegador).

Fluxo: IdP (j_username/j_password) -> SAMLResponse -> SP -> launch.php, cuja resposta é um
redirect para moodlemobile://token=<base64 "siteid:::token[:::privatetoken]">.
"""
import base64
import html
import http.cookiejar
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request

SITE = "https://edisciplinas.usp.br"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


class AuthError(Exception):
    pass


def read_credentials(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    out = {}
    for key in ("user", "pass"):
        m = re.search(rf"^{key}:[ \t]*(.*)$", text, re.M)
        if not m:
            raise AuthError(f"campo '{key}' ausente em {path}")
        v = m.group(1).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[key] = v
    return out["user"], out["pass"]


def parse_token_url(url):
    """moodlemobile://token=<b64> -> token."""
    if "token=" not in url:
        raise AuthError("resposta do launch.php sem token")
    raw = urllib.parse.unquote(url.split("token=", 1)[1])
    raw += "=" * (-len(raw) % 4)
    parts = base64.b64decode(raw).decode().split(":::")
    if len(parts) < 2 or not parts[1]:
        raise AuthError("token ilegível no launch.php")
    return parts[1]


def _form_fields(page):
    return {
        html.unescape(n): html.unescape(v)
        for n, v in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', page)
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def fetch_token(user, password, timeout=30):
    cj = http.cookiejar.CookieJar()
    jar = urllib.request.HTTPCookieProcessor(cj)
    op = urllib.request.build_opener(jar)
    op.addheaders = [("User-Agent", UA)]
    r = op.open(f"{SITE}/auth/shibboleth/index.php", timeout=timeout)
    idp_url = r.geturl()
    page = r.read().decode(errors="replace")
    if "j_username" not in page:
        raise AuthError("formulário do IdP não encontrado")
    action = re.search(r'<form action="([^"]+)"', page)
    form = urllib.parse.urlencode(
        {"j_username": user, "j_password": password, "donotcache": "1", "_eventId_proceed": ""}
    ).encode()
    r = op.open(urllib.parse.urljoin(idp_url, html.unescape(action.group(1)) if action else idp_url), form, timeout=timeout)
    page = r.read().decode(errors="replace")
    if "SAMLResponse" not in page:
        raise AuthError("login SSO recusado (credenciais?)")
    post = re.search(r'<form action="([^"]+)"', page)
    op.open(html.unescape(post.group(1)), urllib.parse.urlencode(_form_fields(page)).encode(), timeout=timeout).read()

    passport = secrets.token_hex(8)
    launch = f"{SITE}/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport={passport}&urlscheme=moodlemobile"
    op2 = urllib.request.build_opener(jar, _NoRedirect)
    op2.addheaders = [("User-Agent", UA)]
    try:
        body = op2.open(launch, timeout=timeout).read().decode(errors="replace")
        m = re.search(r"moodlemobile://token=[A-Za-z0-9+/=%_-]+", body)
        if not m:
            raise AuthError("launch.php sem redirect para moodlemobile://")
        return parse_token_url(m.group(0))
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "")
        if loc.startswith("moodlemobile://"):
            return parse_token_url(loc)
        raise AuthError(f"launch.php respondeu {e.code} (sessão SSO não valeu?)") from None
