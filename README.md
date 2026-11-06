# edisciplinas-downloader

Baixa os materiais (arquivos e pastas) das suas disciplinas do
[e-Disciplinas](https://edisciplinas.usp.br) da USP para uma pasta no seu computador, e
depois baixa só o que for novo ou tiver mudado.

- Usa a mesma API do app oficial do Moodle, com login pela Senha Única (SSO). Não abre navegador.
- Só precisa de Python 3.10+ (biblioteca padrão, sem dependências).
- Os arquivos ficam organizados por semestre e disciplina, com nomes legíveis:
  `2026-2/ABC1234/ABC1234_Aula-3_Slides-Arquivo.pdf`.
- Vídeos ficam de fora por padrão, porque são grandes. Para incluir, use `--videos`.

![edisc cursos --gerar, sync --dryrun e sync baixando os materiais](docs/primeiro-uso.gif)

## Instalação

```sh
pipx install edisciplinas-downloader
```

Sem pipx: `pip install --user edisciplinas-downloader`. Versão em desenvolvimento:
`pipx install git+https://github.com/JoaoVCMiranda/edisciplinas-downloader`. Também dá para
clonar o repo e rodar `python3 -m edisc` de dentro da pasta.

Atualizar: `pipx upgrade edisciplinas-downloader`.

## Primeiro uso

```sh
edisc login --lembrar    # pede Número USP e Senha Única
edisc cursos --gerar     # cria ~/.config/edisciplinas/courses.yaml com suas matrículas
edisc sync --dryrun      # mostra quanto vai baixar, sem baixar nada
edisc sync               # baixa
```

1. **`login`** entra pelo SSO da USP e guarda um token em `~/.config/edisciplinas/token`
   (permissão `0600`). Com `--lembrar`, também guarda usuário e senha (`0600`) para renovar o
   token sozinho quando ele expirar, o que é necessário para rodar por agendamento. Sem
   `--lembrar`, a senha não é guardada; quando o token expirar, o `edisc` pede para você rodar
   `edisc login` de novo.
2. **`cursos --gerar`** lista todas as disciplinas em que você está matriculado (inclusive de
   semestres anteriores) e escreve o `courses.yaml`. **Abra o arquivo e apague o que não quiser
   baixar.** Rodar `--gerar` de novo (num semestre novo, por exemplo) acrescenta só as
   disciplinas novas e mantém as que você já tinha. A versão anterior vai para `courses.yaml.bak`.
   Sem `--gerar`, o comando só lista as matrículas.
3. **`sync`** baixa para `~/edisciplinas` (ou para `EDISC_DIR`, ou `--pasta`). Use
   `edisc sync ABC1234` para uma disciplina só. Disciplinas já concluídas só são conferidas
   uma vez por semana.

### O `courses.yaml`

```yaml
courses:
  - id: ABC1234                       # prefixo dos arquivos
    url: https://edisciplinas.usp.br/course/view.php?id=000000
    destination: 2026-2/ABC1234       # relativo à pasta de downloads, ou caminho absoluto
    status: em_curso                  # opcional: em_curso | concluida
    sincronizar: semanal              # opcional: só confere 1x por semana
```

Veja [`courses.example.yaml`](courses.example.yaml).

## Rodar sozinho

Depois da primeira vez, cada `edisc sync` só baixa o que for novo ou tiver mudado:

![edisc sync sem nada novo](docs/rodada-seguinte.gif)

### systemd (Linux)

```sh
mkdir -p ~/.config/systemd/user
cp contrib/edisc.service contrib/edisc.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now edisc.timer
journalctl --user -u edisc -f        # acompanhar
```

O timer roda a cada 3 horas. Se o `edisc` não estiver em `~/.local/bin`, ajuste o `ExecStart`
(veja `command -v edisc`). Para rodar mesmo sem você estar logado na máquina:
`loginctl enable-linger $USER`.

### cron (Linux/macOS)

```cron
17 */3 * * * $HOME/.local/bin/edisc sync >> $HOME/.local/state/edisciplinas/cron.log 2>&1
```

Nos dois casos, faça o `login` com `--lembrar`, ou o token vai expirar e o sync vai parar.

## Onde fica cada coisa

| o quê | onde | variável |
|---|---|---|
| downloads | `~/edisciplinas` | `EDISC_DIR` |
| `courses.yaml`, token, credenciais | `~/.config/edisciplinas/` | `EDISC_CONFIG` (ou `EDISC_COURSES` só para o yaml) |
| estado (o que já foi baixado) | `~/.local/state/edisciplinas/` | `EDISC_STATE` |

Para esquecer tudo: apague `~/.config/edisciplinas` e `~/.local/state/edisciplinas`.

## Como funciona

- **Login**: o e-Disciplinas usa Shibboleth (SAML), então o `login/token.php` padrão do
  Moodle não funciona. O `edisc` faz o fluxo SAML por HTTP (IdP → SP) e chama
  `admin/tool/mobile/launch.php?service=moodle_mobile_app`, como o app oficial. O redirect
  `moodlemobile://token=…` traz o token.
- **Sync**: `core_course_get_contents` em cada disciplina. Recursos (`resource`) e pastas
  (`folder`) são baixados, e o resto (fóruns, tarefas, links etc.) não. É incremental por
  `timemodified`: se o professor substituir um arquivo, a versão nova é baixada por cima.
- Arquivo que já estava na pasta com o mesmo nome e tamanho é adotado, sem baixar de novo.
  Arquivo que o servidor recusa (403/404) é anotado e não é tentado a cada rodada. Falhas de
  rede são tentadas de novo (3x, com espera).
- Saída: código `0` se deu tudo certo, `1` se alguma disciplina falhou, `2` se o login falhou.

Isto não é um projeto oficial da USP. Use com bom senso: o intervalo de 3 horas do timer é
mais que suficiente.

## Desenvolvimento

```sh
python3 -m unittest discover -s tests -t .
```

Para publicar uma versão: suba `__version__` em `edisc/__init__.py`, faça o commit e crie a tag
`git tag v0.X.Y && git push --tags`. O workflow `publica.yml` roda os testes, gera o wheel e o
sdist e publica no PyPI por Trusted Publishing.

Licença: MIT.
