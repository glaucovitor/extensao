"""
Envio de Certificados de Extensão — FCTE/UnB
Flask + SQLite

Execução rápida (desenvolvimento):
    pip install -r requirements.txt
    flask --app app criar-superusuario admin@unb.br "Nome do Administrador"
    flask --app app run --debug
"""
import os
import re
import secrets
import sqlite3
import unicodedata
import uuid
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

import click
import io
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
TZ = ZoneInfo("America/Sao_Paulo")


def carrega_env(caminho=BASE_DIR / ".env"):
    """Lê o arquivo .env (CHAVE=valor, uma por linha) e coloca os valores nas
    variáveis de ambiente. Variáveis já definidas no sistema têm prioridade.
    Linhas vazias e iniciadas por # são ignoradas."""
    if not caminho.exists():
        return
    for linha in caminho.read_text(encoding="utf-8-sig").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        chave, valor = chave.strip(), valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        if chave and valor and chave not in os.environ:
            os.environ[chave] = valor


def chave_secreta():
    """SECRET_KEY do .env/ambiente; se não houver, cria uma e guarda em
    instance/secret_key, para que as sessões não caiam a cada reinício."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    arquivo = BASE_DIR / "instance" / "secret_key"
    try:
        if arquivo.exists():
            return arquivo.read_text().strip()
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        chave = secrets.token_hex(32)
        arquivo.write_text(chave)
        return chave
    except OSError:
        return secrets.token_hex(32)


carrega_env()

# ---------------------------------------------------------------------------
# Configuração (tudo pode ser sobrescrito por variáveis de ambiente)
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config.update(
    SECRET_KEY=chave_secreta(),
    DATABASE=os.environ.get("DATABASE", str(BASE_DIR / "instance" / "certificados.db")),
    UPLOAD_FOLDER=os.environ.get("UPLOAD_FOLDER", str(BASE_DIR / "uploads")),
    # Cloudflare R2: com as quatro variáveis definidas, os PDFs são guardados no
    # bucket do R2 em vez da pasta local (UPLOAD_FOLDER).
    R2_ACCOUNT_ID=os.environ.get("R2_ACCOUNT_ID"),
    R2_ACCESS_KEY_ID=os.environ.get("R2_ACCESS_KEY_ID"),
    R2_SECRET_ACCESS_KEY=os.environ.get("R2_SECRET_ACCESS_KEY"),
    R2_BUCKET=os.environ.get("R2_BUCKET"),
    R2_ENDPOINT=os.environ.get("R2_ENDPOINT"),  # opcional; padrão https://<conta>.r2.cloudflarestorage.com
    # 25 MB por arquivo; o limite da requisição tem uma folga para os demais campos
    MAX_PDF_BYTES=25 * 1024 * 1024,
    MAX_CONTENT_LENGTH=26 * 1024 * 1024,
    # E-mail institucional do aluno, montado a partir da matrícula
    # Domínio exigido para o e-mail institucional (login) dos servidores
    DOMINIO_PROFESSOR=os.environ.get("DOMINIO_PROFESSOR", "unb.br"),
    # Contas de teste (senha 123456): aluno 2025999, teste@unb.br (colegiado),
    # coordenador@unb.br (coordenação) e admin@unb.br (superusuário).
    # Em produção, defina CONTAS_TESTE=0 para desativá-las.
    CONTAS_TESTE=os.environ.get("CONTAS_TESTE", "1") == "1",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
)

STATUS = {
    "aguardando": "Aguardando avaliação do colegiado de extensão",
    "validado": "Validado",
    "recusado": "Recusado",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS alunos (
    matricula   TEXT PRIMARY KEY,
    nome        TEXT NOT NULL,
    curso       TEXT NOT NULL,
    curriculo   TEXT,
    senha_hash  TEXT,           -- não é mais usado (o login do aluno passou a ser matrícula + CPF)
    cpf_hash    TEXT,           -- CPF guardado como hash, como uma senha
    ativo       INTEGER NOT NULL DEFAULT 1
);
-- Servidores: professores do colegiado (avaliador), coordenadores de curso e superusuários.
-- Uma mesma pessoa pode ter mais de um papel.
CREATE TABLE IF NOT EXISTS professores (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT NOT NULL UNIQUE,
    nome         TEXT NOT NULL,
    senha_hash   TEXT,
    ativo        INTEGER NOT NULL DEFAULT 1,
    avaliador    INTEGER NOT NULL DEFAULT 1,
    coordenador  INTEGER NOT NULL DEFAULT 0,
    superusuario INTEGER NOT NULL DEFAULT 0,
    trocar_senha INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS coordenador_cursos (
    usuario_id INTEGER NOT NULL REFERENCES professores(id) ON DELETE CASCADE,
    curso      TEXT NOT NULL,
    PRIMARY KEY (usuario_id, curso)
);
CREATE TABLE IF NOT EXISTS certificados (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    matricula     TEXT NOT NULL REFERENCES alunos(matricula),
    arquivo       TEXT NOT NULL,
    nome_original TEXT NOT NULL,
    horas         REAL NOT NULL,
    data_inicio   TEXT NOT NULL,
    data_fim      TEXT NOT NULL,
    atuacao       TEXT NOT NULL CHECK (atuacao = 'participante'),  -- "participante" = membro da equipe organizadora
    status        TEXT NOT NULL DEFAULT 'aguardando'
                  CHECK (status IN ('aguardando','validado','recusado')),
    motivo        TEXT,
    horas_validadas REAL,
    reuniao       TEXT,
    lancado_em    TEXT,
    lancado_por   INTEGER REFERENCES professores(id),
    numero        INTEGER,      -- nº fixo do certificado do aluno (usado no nome do arquivo)
    enviado_em    TEXT NOT NULL,
    avaliado_em   TEXT,
    avaliado_por  INTEGER REFERENCES professores(id)
);
CREATE INDEX IF NOT EXISTS idx_cert_matricula ON certificados(matricula);
CREATE INDEX IF NOT EXISTS idx_cert_status ON certificados(status);
CREATE TABLE IF NOT EXISTS meta (
    chave TEXT PRIMARY KEY,
    valor TEXT
);
"""


# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    db = get_db()
    db.executescript(SCHEMA)
    # Migração de bancos criados na versão anterior
    colunas = {r["name"] for r in db.execute("PRAGMA table_info(certificados)")}
    if "horas_validadas" not in colunas:
        db.execute("ALTER TABLE certificados ADD COLUMN horas_validadas REAL")
        db.execute("UPDATE certificados SET horas_validadas = horas WHERE status = 'validado'")
    if "reuniao" not in colunas:
        db.execute("ALTER TABLE certificados ADD COLUMN reuniao TEXT")
    if "numero" not in colunas:
        db.execute("ALTER TABLE certificados ADD COLUMN numero INTEGER")
    if "lancado_em" not in colunas:
        db.execute("ALTER TABLE certificados ADD COLUMN lancado_em TEXT")
        db.execute("ALTER TABLE certificados ADD COLUMN lancado_por INTEGER REFERENCES professores(id)")
    colunas = {r["name"] for r in db.execute("PRAGMA table_info(professores)")}
    for col, padrao in (("avaliador", 1), ("coordenador", 0), ("superusuario", 0), ("trocar_senha", 0)):
        if col not in colunas:
            db.execute(f"ALTER TABLE professores ADD COLUMN {col} INTEGER NOT NULL DEFAULT {padrao}")
    if "cpf_hash" not in {r["name"] for r in db.execute("PRAGMA table_info(alunos)")}:
        db.execute("ALTER TABLE alunos ADD COLUMN cpf_hash TEXT")
    if db.execute("SELECT 1 FROM meta WHERE chave = 'sem_planilha'").fetchone() is None:
        # Versões anteriores desativavam quem saía da planilha; agora todos têm acesso.
        db.execute("UPDATE alunos SET ativo = 1")
        db.execute("INSERT INTO meta (chave, valor) VALUES ('sem_planilha', '1')")
    garante_contas_teste(db)
    if db.execute("SELECT 1 FROM meta WHERE chave = 'nomes_arquivos'").fetchone() is None:
        # Arquivos enviados em versões anteriores passam a seguir o padrão matrícula_curso_n.pdf
        for r in db.execute("SELECT DISTINCT matricula FROM certificados").fetchall():
            renomeia_arquivos_do_aluno(db, r["matricula"])
        db.execute("INSERT INTO meta (chave, valor) VALUES ('nomes_arquivos', '1')")
    db.commit()


ALUNO_TESTE = ("2025999", "João de Teste", "Engenharia de Software", "6360/2")
CPF_TESTE = "52998224725"  # CPF fictício, com dígitos verificadores válidos
# (e-mail, nome, avaliador, coordenador, superusuário, cursos coordenados)
SERVIDORES_TESTE = [
    ("teste@unb.br", "Professor Teste", 1, 0, 0, []),
    ("coordenador@unb.br", "Coordenador Teste", 0, 1, 0, ["Engenharia de Software"]),
    ("admin@unb.br", "Administrador Teste", 0, 0, 1, []),
]
SENHA_TESTE = "123456"


def garante_contas_teste(db):
    """Cria as contas de teste uma única vez. Depois disso, elas podem ser editadas
    ou renomeadas normalmente; só voltam a ser tocadas quando CONTAS_TESTE muda
    (CONTAS_TESTE=0 desativa, CONTAS_TESTE=1 reativa)."""
    desejado = "1" if app.config["CONTAS_TESTE"] else "0"
    if app.config["CONTAS_TESTE"] and db.execute(
            "SELECT 1 FROM meta WHERE chave = 'cpf_teste'").fetchone() is None:
        # Bancos anteriores: o aluno de teste passa a entrar com o CPF de teste.
        db.execute("UPDATE alunos SET cpf_hash = ? WHERE matricula = ? AND cpf_hash IS NULL",
                   (hash_cpf(CPF_TESTE), ALUNO_TESTE[0]))
        db.execute("INSERT INTO meta (chave, valor) VALUES ('cpf_teste', '1')")
    salvo = db.execute("SELECT valor FROM meta WHERE chave = 'contas_teste'").fetchone()
    if salvo is not None and salvo["valor"] == desejado:
        return
    ativo = int(desejado)
    mat, nome, curso, curr = ALUNO_TESTE
    if salvo is None:  # primeira execução: cria o que ainda não existir
        if db.execute("SELECT 1 FROM alunos WHERE matricula = ?", (mat,)).fetchone() is None:
            db.execute("""INSERT INTO alunos (matricula, nome, curso, curriculo, cpf_hash, ativo)
                          VALUES (?, ?, ?, ?, ?, ?)""",
                       (mat, nome, curso, curr, hash_cpf(CPF_TESTE), ativo))
        for email, pnome, aval, coord, sup, cursos in SERVIDORES_TESTE:
            if db.execute("SELECT 1 FROM professores WHERE email = ?", (email,)).fetchone() is None:
                cur = db.execute(
                    """INSERT INTO professores (email, nome, senha_hash, ativo, avaliador,
                           coordenador, superusuario) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (email, pnome, generate_password_hash(SENHA_TESTE), ativo, aval, coord, sup))
                for c in cursos:
                    db.execute("INSERT INTO coordenador_cursos (usuario_id, curso) VALUES (?, ?)",
                               (cur.lastrowid, c))
    db.execute("UPDATE alunos SET ativo = ? WHERE matricula = ?", (ativo, mat))
    for email, *_ in SERVIDORES_TESTE:
        db.execute("UPDATE professores SET ativo = ? WHERE email = ?", (ativo, email))
    db.execute("INSERT OR REPLACE INTO meta (chave, valor) VALUES ('contas_teste', ?)", (desejado,))


def agora():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Dados do aluno (informados por ele no primeiro acesso)
# ---------------------------------------------------------------------------
def normaliza_matricula(valor):
    """Usada apenas em buscas e comandos de terminal; nos formulários do aluno a
    matrícula precisa vir só com números (ver valida_dados_aluno)."""
    if valor is None:
        return ""
    return re.sub(r"\D", "", str(valor).strip())


def so_digitos(valor):
    """Devolve o valor sem espaços nas pontas, ou None se tiver algo além de números."""
    valor = (valor or "").strip()
    return valor if valor.isascii() and valor.isdigit() else None


def cpf_valido(cpf):
    """Confere os dois dígitos verificadores do CPF (11 números)."""
    if not cpf or len(cpf) != 11 or not cpf.isdigit() or cpf == cpf[0] * 11:
        return False
    for n in (9, 10):
        soma = sum(int(cpf[i]) * (n + 1 - i) for i in range(n))
        dv = (soma * 10) % 11 % 10
        if dv != int(cpf[n]):
            return False
    return True


def hash_cpf(cpf):
    return generate_password_hash(cpf)


def valida_dados_aluno(f, exigir_cpf=True):
    """Valida os dados do aluno (primeiro acesso e área do superusuário).
    Matrícula e CPF são aceitos apenas com números. Devolve (dados, erros)."""
    matricula = so_digitos(f.get("matricula"))
    cpf_bruto = (f.get("cpf") or "").strip()
    cpf = so_digitos(cpf_bruto)
    dados = {
        "matricula": matricula or (f.get("matricula") or "").strip()[:20],
        "nome": " ".join(f.get("nome", "").split())[:150],
        "curso": f.get("curso", ""),
        "curriculo": f.get("curriculo", "").strip()[:30],
        "cpf": cpf or "",
    }
    erros = []
    if matricula is None:
        erros.append("A matrícula deve conter somente números (sem pontos, barras ou letras).")
    elif not (5 <= len(matricula) <= 12):
        erros.append("Informe uma matrícula válida.")
    if cpf_bruto or exigir_cpf:
        if cpf is None:
            erros.append("O CPF deve conter somente os 11 números, sem pontos ou traço.")
        elif not cpf_valido(cpf):
            erros.append("CPF inválido. Confira os 11 números informados.")
    if len(dados["nome"]) < 3:
        erros.append("Informe seu nome completo.")
    if dados["curso"] not in CURSOS_FCTE:
        erros.append("Selecione o seu curso.")
    if not dados["curriculo"]:
        erros.append("Informe o número do seu currículo (ex.: 6360/2).")
    return dados, erros


# ---------------------------------------------------------------------------
# Segurança: CSRF e sessões
# ---------------------------------------------------------------------------
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["STATUS"] = STATUS


@app.before_request
def antes():
    init_db()
    if request.method == "POST":
        token = request.form.get("csrf_token", "")
        if not token or not secrets.compare_digest(token, session.get("_csrf", "")):
            abort(400, "Sessão expirada. Recarregue a página e tente novamente.")


def senha_valida(senha):
    if not senha:
        return "Informe uma senha."
    return None


def login_aluno_requerido(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        mat = session.get("aluno")
        if not mat:
            return redirect(url_for("login"))
        aluno = get_db().execute(
            "SELECT * FROM alunos WHERE matricula = ? AND ativo = 1", (mat,)
        ).fetchone()
        if aluno is None:
            session.clear()
            flash("Sua sessão foi encerrada porque seus dados de acesso foram alterados ou bloqueados. "
                  "Entre novamente.", "erro")
            return redirect(url_for("login"))
        g.aluno = aluno
        return view(*args, **kwargs)
    return wrapper


# Cursos da FCTE que podem ter coordenador(a) vinculado(a)
CURSOS_FCTE = [
    "Engenharia Automotiva",
    "Engenharia Aeroespacial",
    "Engenharia Eletrônica",
    "Engenharia de Energia",
    "Engenharia de Software",
]

# Nome de cada perfil exibido no cadastro e na lista de usuários
NOMES_PERFIS = {
    "avaliador": "Membro do colegiado de extensão",
    "coordenador": "Coordenador(a) de curso",
    "superusuario": "Superusuário",
}

PAPEIS = {
    "avaliador": ("Colegiado de extensão", "admin_painel"),
    "coordenador": ("Coordenação de curso", "coord_painel"),
    "superusuario": ("Administração", "super_painel"),
}


def area_inicial(usuario):
    for papel, (_rotulo, endpoint) in PAPEIS.items():
        if usuario[papel]:
            return url_for(endpoint)
    return None


def carrega_servidor():
    uid = session.get("professor")
    usuario = get_db().execute(
        "SELECT * FROM professores WHERE id = ? AND ativo = 1", (uid,)
    ).fetchone() if uid else None
    if usuario is None:
        session.pop("professor", None)
    return usuario


def servidor_requerido(papel):
    """Exige login de servidor com o papel indicado (avaliador, coordenador
    ou superusuario). Quem ainda está com a senha inicial é levado a trocá-la."""
    def decorador(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            usuario = carrega_servidor()
            if usuario is None:
                return redirect(url_for("admin_login"))
            if usuario["trocar_senha"]:
                return redirect(url_for("trocar_senha"))
            if not usuario[papel]:
                flash("Seu perfil não tem acesso a esta área.", "erro")
                return redirect(area_inicial(usuario) or url_for("admin_sair"))
            g.usuario = g.professor = usuario
            g.cursos_coordenados = [r["curso"] for r in get_db().execute(
                "SELECT curso FROM coordenador_cursos WHERE usuario_id = ? ORDER BY curso",
                (usuario["id"],))]
            return view(*args, **kwargs)
        return wrapper
    return decorador


login_professor_requerido = servidor_requerido("avaliador")
app.jinja_env.globals["PAPEIS"] = PAPEIS
app.jinja_env.globals["NOMES_PERFIS"] = NOMES_PERFIS


@app.errorhandler(413)
def arquivo_grande(_e):
    flash("O arquivo excede o limite permitido (25 MB).", "erro")
    return redirect(request.referrer or url_for("login"))


# ---------------------------------------------------------------------------
# Armazenamento dos PDFs: pasta local ou Cloudflare R2
# ---------------------------------------------------------------------------
class ArmazenamentoLocal:
    descricao = "pasta local"

    def __init__(self, pasta):
        self.pasta = Path(pasta)
        self.pasta.mkdir(parents=True, exist_ok=True)

    def salvar(self, nome, arquivo):
        arquivo.seek(0)
        with open(self.pasta / nome, "wb") as f:
            while bloco := arquivo.read(1024 * 1024):
                f.write(bloco)

    def existe(self, nome):
        return (self.pasta / nome).exists()

    def renomear(self, de, para):
        if self.existe(de):
            os.replace(self.pasta / de, self.pasta / para)

    def apagar(self, nome):
        try:
            os.remove(self.pasta / nome)
        except FileNotFoundError:
            pass

    def resposta(self, nome, nome_download):
        return send_from_directory(self.pasta, nome, download_name=nome_download,
                                   mimetype="application/pdf")


class ArmazenamentoR2:
    """Cloudflare R2 (API compatível com S3). O bucket fica privado: os PDFs
    passam sempre pelo sistema, que confere quem pode ver cada arquivo."""
    descricao = "Cloudflare R2"

    def __init__(self, cfg):
        import boto3
        from botocore.config import Config
        self.bucket = cfg["R2_BUCKET"]
        endpoint = cfg["R2_ENDPOINT"] or f"https://{cfg['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
        self.s3 = boto3.client(
            "s3", endpoint_url=endpoint, region_name="auto",
            aws_access_key_id=cfg["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=cfg["R2_SECRET_ACCESS_KEY"],
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}))

    def salvar(self, nome, arquivo):
        arquivo.seek(0)
        self.s3.upload_fileobj(arquivo, self.bucket, nome,
                               ExtraArgs={"ContentType": "application/pdf"})

    def existe(self, nome):
        from botocore.exceptions import ClientError
        try:
            self.s3.head_object(Bucket=self.bucket, Key=nome)
            return True
        except ClientError:
            return False

    def renomear(self, de, para):
        if self.existe(de):
            self.s3.copy_object(Bucket=self.bucket, Key=para,
                                CopySource={"Bucket": self.bucket, "Key": de},
                                ContentType="application/pdf", MetadataDirective="REPLACE")
            self.s3.delete_object(Bucket=self.bucket, Key=de)

    def apagar(self, nome):
        self.s3.delete_object(Bucket=self.bucket, Key=nome)

    def resposta(self, nome, nome_download):
        from botocore.exceptions import ClientError
        from flask import Response
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=nome)
        except ClientError:
            abort(404)
        return Response(
            obj["Body"].iter_chunks(64 * 1024), mimetype="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{nome_download}"',
                     "Content-Length": str(obj["ContentLength"]),
                     "Cache-Control": "private, no-store"})


def r2_configurado(cfg=None):
    cfg = cfg or app.config
    return all(cfg.get(k) for k in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")) and \
        bool(cfg.get("R2_ACCOUNT_ID") or cfg.get("R2_ENDPOINT"))


_armazenamento = {}


def armazenamento():
    """Devolve o armazenamento em uso (R2 se configurado; senão, a pasta local)."""
    chave = ("r2", app.config.get("R2_BUCKET"), app.config.get("R2_ENDPOINT"),
             app.config.get("R2_ACCOUNT_ID")) if r2_configurado() else ("local", app.config["UPLOAD_FOLDER"])
    if chave not in _armazenamento:
        _armazenamento[chave] = (ArmazenamentoR2(app.config) if chave[0] == "r2"
                                 else ArmazenamentoLocal(app.config["UPLOAD_FOLDER"]))
    return _armazenamento[chave]


# ---------------------------------------------------------------------------
# Nome dos arquivos: <matrícula>_<curso>_<nº>.pdf
# ---------------------------------------------------------------------------
def trecho_curso(curso):
    """'Engenharia Eletrônica' -> 'Engenharia_Eletronica' (sem acentos nem espaços)."""
    sem_acento = unicodedata.normalize("NFKD", curso or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", sem_acento).strip("_") or "Curso"


def renomeia_arquivos_do_aluno(db, matricula):
    """Dá a todos os certificados do aluno o nome <matrícula>_<curso>_<n>.pdf.
    O número de cada certificado é fixo: excluir um não renumera os outros. Usado no envio e quando a matrícula ou o
    curso do aluno mudam. Renomeia em duas etapas para evitar colisões."""
    aluno = db.execute("SELECT curso FROM alunos WHERE matricula = ?", (matricula,)).fetchone()
    if aluno is None:
        return
    arm = armazenamento()
    # Certificados ainda sem número (novos ou de versões anteriores) recebem o próximo livre.
    proximo = db.execute("SELECT COALESCE(MAX(numero), 0) FROM certificados WHERE matricula = ?",
                         (matricula,)).fetchone()[0]
    for r in db.execute("SELECT id FROM certificados WHERE matricula = ? AND numero IS NULL "
                        "ORDER BY id", (matricula,)).fetchall():
        proximo += 1
        db.execute("UPDATE certificados SET numero = ? WHERE id = ?", (proximo, r["id"]))
    certs = db.execute("SELECT id, arquivo, numero FROM certificados WHERE matricula = ? ORDER BY id",
                       (matricula,)).fetchall()
    base = f"{matricula}_{trecho_curso(aluno['curso'])}"
    alvos = {c["id"]: f"{base}_{c['numero']}.pdf" for c in certs}
    pendentes = [c for c in certs if c["arquivo"] != alvos[c["id"]]]
    temporarios = {}
    for c in pendentes:  # 1ª etapa: nomes temporários
        tmp = f".renomeando-{uuid.uuid4().hex}.pdf"
        arm.renomear(c["arquivo"], tmp)
        temporarios[c["id"]] = tmp
    for c in pendentes:  # 2ª etapa: nomes finais
        final = alvos[c["id"]]
        arm.renomear(temporarios[c["id"]], final)
        db.execute("UPDATE certificados SET arquivo = ?, nome_original = ? WHERE id = ?",
                   (final, final, c["id"]))
    for c in certs:  # garante nome exibido = nome do arquivo
        if c["id"] not in temporarios:
            db.execute("UPDATE certificados SET nome_original = ? WHERE id = ?",
                       (alvos[c["id"]], c["id"]))


# ---------------------------------------------------------------------------
# Área do aluno
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def login():
    if session.get("aluno"):
        return redirect(url_for("painel_aluno"))
    if request.method == "POST":
        mat = so_digitos(request.form.get("matricula"))
        cpf = so_digitos(request.form.get("cpf"))
        aluno = get_db().execute(
            "SELECT * FROM alunos WHERE matricula = ?", (mat,)
        ).fetchone() if mat else None
        if mat is None or cpf is None:
            flash("Informe a matrícula e o CPF usando somente números.", "erro")
        elif aluno is not None and not aluno["ativo"]:
            flash(MSG_BLOQUEADO, "erro")
        elif aluno is None:
            flash("Matrícula ainda não cadastrada. Faça o seu primeiro acesso.", "info")
            return redirect(url_for("primeiro_acesso", matricula=mat))
        elif not aluno["cpf_hash"]:
            flash("Seu cadastro ainda não tem CPF. Complete-o no primeiro acesso.", "info")
            return redirect(url_for("primeiro_acesso", matricula=mat))
        elif check_password_hash(aluno["cpf_hash"], cpf):
            session.clear()
            session["aluno"] = mat
            return redirect(url_for("painel_aluno"))
        else:
            flash("Matrícula ou CPF incorretos.", "erro")
    return render_template("login.html")


MSG_BLOQUEADO = ("O acesso desta matrícula está bloqueado. "
                 "Procure a coordenação de extensão da FCTE.")


@app.route("/primeiro-acesso", methods=["GET", "POST"])
def primeiro_acesso():
    """Cadastro do aluno: nome, matrícula, curso, currículo e CPF. Ao concluir,
    o aluno já entra no sistema. Depois, o acesso é sempre por matrícula + CPF."""
    dados = {"matricula": so_digitos(request.args.get("matricula")) or ""}
    if request.method == "POST":
        dados, erros = valida_dados_aluno(request.form)
        db = get_db()
        existente = db.execute("SELECT * FROM alunos WHERE matricula = ?",
                               (dados["matricula"],)).fetchone()
        if not erros and existente and not existente["ativo"]:
            flash(MSG_BLOQUEADO, "erro")
            return redirect(url_for("login"))
        if not erros and existente and existente["cpf_hash"]:
            flash("Esta matrícula já está cadastrada. Entre com sua matrícula e seu CPF. "
                  "Se houver algum problema, procure a coordenação de extensão da FCTE.", "info")
            return redirect(url_for("login"))
        if erros:
            for e in erros:
                flash(e, "erro")
        else:
            with db:
                db.execute(
                    """INSERT INTO alunos (matricula, nome, curso, curriculo, cpf_hash, ativo)
                       VALUES (?, ?, ?, ?, ?, 1)
                       ON CONFLICT(matricula) DO UPDATE SET nome = excluded.nome,
                         curso = excluded.curso, curriculo = excluded.curriculo,
                         cpf_hash = excluded.cpf_hash, ativo = 1""",
                    (dados["matricula"], dados["nome"], dados["curso"], dados["curriculo"],
                     hash_cpf(dados["cpf"])))
            session.clear()
            session["aluno"] = dados["matricula"]
            flash("Cadastro concluído! Nos próximos acessos, entre com sua matrícula e seu CPF.", "ok")
            return redirect(url_for("painel_aluno"))
    dados.pop("cpf", None)  # o CPF nunca volta preenchido para a tela
    return render_template("primeiro_acesso.html", dados=dados, cursos=CURSOS_FCTE)


@app.route("/painel")
@login_aluno_requerido
def painel_aluno():
    db = get_db()
    certs = db.execute(
        "SELECT * FROM certificados WHERE matricula = ? ORDER BY enviado_em DESC",
        (g.aluno["matricula"],),
    ).fetchall()
    totais = db.execute(
        """SELECT COALESCE(SUM(CASE WHEN status='validado' THEN horas_validadas END), 0) AS validadas,
                  COALESCE(SUM(CASE WHEN status='aguardando' THEN horas END), 0) AS pendentes
           FROM certificados WHERE matricula = ?""",
        (g.aluno["matricula"],),
    ).fetchone()
    return render_template("painel_aluno.html", aluno=g.aluno, certs=certs,
                           totais=totais, hoje=date.today().isoformat())


@app.route("/enviar", methods=["POST"])
@login_aluno_requerido
def enviar_certificado():
    f = request.form
    erros = []

    atuacao = f.get("atuacao")
    if atuacao == "ouvinte":
        erros.append("Somente certificados em que você atuou como membro da equipe organizadora "
                     "do evento são aceitos. Certificados de ouvinte não podem ser enviados.")
    elif atuacao != "participante":
        erros.append("Informe como você atuou no evento.")

    try:
        horas = float(f.get("horas", "").replace(",", "."))
        if not (0 < horas <= 1000):
            raise ValueError
    except ValueError:
        horas = None
        erros.append("Informe uma quantidade de horas válida (maior que zero).")

    try:
        ini = date.fromisoformat(f.get("data_inicio", ""))
        fim = date.fromisoformat(f.get("data_fim", ""))
        if fim < ini:
            erros.append("A data de término não pode ser anterior à data de início.")
        if fim > date.today():
            erros.append("A data de término não pode estar no futuro.")
    except ValueError:
        ini = fim = None
        erros.append("Informe as datas de início e término.")

    arq = request.files.get("arquivo")
    if not arq or not arq.filename:
        erros.append("Selecione o arquivo PDF do certificado.")
    else:
        arq.stream.seek(0, os.SEEK_END)
        tamanho = arq.stream.tell()
        arq.stream.seek(0)
        cabecalho = arq.stream.read(5)
        arq.stream.seek(0)
        if not arq.filename.lower().endswith(".pdf") or cabecalho != b"%PDF-":
            erros.append("O arquivo precisa estar em formato PDF.")
        elif tamanho > app.config["MAX_PDF_BYTES"]:
            erros.append("O arquivo excede o limite de 25 MB.")
        elif tamanho == 0:
            erros.append("O arquivo enviado está vazio.")

    if erros:
        for e in erros:
            flash(e, "erro")
        return redirect(url_for("painel_aluno"))

    nome_temp = f".envio-{uuid.uuid4().hex}.pdf"
    try:
        armazenamento().salvar(nome_temp, arq.stream)
    except Exception:
        app.logger.exception("Falha ao guardar o PDF (%s)", armazenamento().descricao)
        flash("Não foi possível guardar o arquivo agora. Tente novamente em alguns minutos.", "erro")
        return redirect(url_for("painel_aluno"))
    db = get_db()
    with db:
        db.execute(
            """INSERT INTO certificados (matricula, arquivo, nome_original, horas,
                   data_inicio, data_fim, atuacao, enviado_em)
               VALUES (?, ?, ?, ?, ?, ?, 'participante', ?)""",
            (g.aluno["matricula"], nome_temp, nome_temp,
             horas, ini.isoformat(), fim.isoformat(), agora()),
        )
        renomeia_arquivos_do_aluno(db, g.aluno["matricula"])
    nome = db.execute("SELECT nome_original FROM certificados WHERE matricula = ? "
                      "ORDER BY id DESC LIMIT 1", (g.aluno["matricula"],)).fetchone()[0]
    flash(f"Certificado enviado como {nome}! Ele aguardará avaliação do colegiado de extensão.", "ok")
    return redirect(url_for("painel_aluno"))


@app.route("/certificado/<int:cid>/arquivo")
@login_aluno_requerido
def arquivo_aluno(cid):
    c = get_db().execute("SELECT * FROM certificados WHERE id = ? AND matricula = ?",
                         (cid, g.aluno["matricula"])).fetchone()
    if c is None:
        abort(404)
    return armazenamento().resposta(c["arquivo"], c["nome_original"])


@app.route("/certificado/<int:cid>/excluir", methods=["POST"])
@login_aluno_requerido
def excluir_certificado(cid):
    """O aluno pode excluir certificados aguardando avaliação ou recusados.
    Certificados validados não podem ser excluídos."""
    db = get_db()
    c = db.execute("SELECT * FROM certificados WHERE id = ? AND matricula = ?",
                   (cid, g.aluno["matricula"])).fetchone()
    if c is None:
        abort(404)
    if c["status"] == "validado":
        flash("Certificados já validados pelo colegiado de extensão não podem ser excluídos.", "erro")
        return redirect(url_for("painel_aluno"))
    with db:
        db.execute("DELETE FROM certificados WHERE id = ?", (cid,))
    try:
        armazenamento().apagar(c["arquivo"])
    except Exception:
        app.logger.exception("Falha ao apagar %s", c["arquivo"])
    flash(f"Certificado {c['nome_original']} excluído.", "ok")
    return redirect(url_for("painel_aluno"))


@app.route("/sair")
def sair():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Área administrativa (professores credenciados)
# ---------------------------------------------------------------------------
@app.route("/colegiado", methods=["GET", "POST"])
def admin_login():
    if session.get("professor"):
        u = get_db().execute("SELECT * FROM professores WHERE id = ? AND ativo = 1",
                             (session["professor"],)).fetchone()
        if u and area_inicial(u):
            return redirect(area_inicial(u))
        session.pop("professor", None)
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        prof = get_db().execute(
            "SELECT * FROM professores WHERE email = ? AND ativo = 1", (email,)
        ).fetchone()
        if prof and prof["senha_hash"] and check_password_hash(prof["senha_hash"], senha):
            if not area_inicial(prof):
                flash("Seu cadastro não tem nenhum perfil de acesso. Procure a administração.", "erro")
                return render_template("admin_login.html")
            session.clear()
            session["professor"] = prof["id"]
            if prof["trocar_senha"]:
                flash("Este é o seu primeiro acesso. Para continuar, crie uma nova senha.", "info")
                return redirect(url_for("trocar_senha"))
            return redirect(area_inicial(prof))
        flash("E-mail ou senha incorretos, ou servidor não credenciado.", "erro")
    return render_template("admin_login.html")


@app.route("/colegiado/trocar-senha", methods=["GET", "POST"])
def trocar_senha():
    """Troca de senha do servidor: obrigatória no primeiro acesso (senha inicial
    definida pelo superusuário) e disponível a qualquer momento pelo menu."""
    usuario = carrega_servidor()
    if usuario is None:
        return redirect(url_for("admin_login"))
    g.usuario = usuario
    obrigatoria = bool(usuario["trocar_senha"])
    if request.method == "POST":
        atual = request.form.get("atual", "")
        nova = request.form.get("senha", "")
        erro = None
        if not check_password_hash(usuario["senha_hash"] or "", atual):
            erro = "A senha atual está incorreta."
        elif senha_valida(nova):
            erro = senha_valida(nova)
        elif nova != request.form.get("confirmacao", ""):
            erro = "As senhas não coincidem."
        elif nova == atual:
            erro = "A nova senha deve ser diferente da atual."
        if erro:
            flash(erro, "erro")
        else:
            db = get_db()
            with db:
                db.execute("UPDATE professores SET senha_hash = ?, trocar_senha = 0 WHERE id = ?",
                           (generate_password_hash(nova), usuario["id"]))
            flash("Senha alterada com sucesso.", "ok")
            return redirect(area_inicial(usuario) or url_for("admin_login"))
    return render_template("trocar_senha.html", obrigatoria=obrigatoria)


@app.route("/colegiado/certificados")
@login_professor_requerido
def admin_painel():
    db = get_db()
    curso = request.args.get("curso", "")
    status = request.args.get("status", "aguardando")
    sql = """SELECT c.*, a.nome, a.curso, a.curriculo, p.nome AS avaliador
             FROM certificados c
             JOIN alunos a ON a.matricula = c.matricula
             LEFT JOIN professores p ON p.id = c.avaliado_por
             WHERE 1 = 1"""
    params = []
    if curso:
        sql += " AND a.curso = ?"
        params.append(curso)
    if status in STATUS:
        sql += " AND c.status = ?"
        params.append(status)
    sql += " ORDER BY c.enviado_em ASC" if status == "aguardando" else " ORDER BY c.enviado_em DESC"
    certs = db.execute(sql, params).fetchall()
    # Agrupa por aluno, mantendo a ordem do envio mais antigo (aguardando) ou mais recente.
    grupos = {}
    for c in certs:
        gr = grupos.setdefault(c["matricula"], {
            "matricula": c["matricula"], "nome": c["nome"], "curso": c["curso"],
            "curriculo": c["curriculo"], "certs": [], "horas": 0.0})
        gr["certs"].append(c)
        gr["horas"] += c["horas"]
    cursos = CURSOS_FCTE
    contagem = {r["status"]: r["n"] for r in db.execute(
        "SELECT status, COUNT(*) AS n FROM certificados GROUP BY status")}
    return render_template("admin_painel.html", grupos=list(grupos.values()), total=len(certs),
                           cursos=cursos, curso=curso, status=status, contagem=contagem)


@app.route("/colegiado/certificado/<int:cid>/arquivo")
@login_professor_requerido
def arquivo_admin(cid):
    c = get_db().execute("SELECT * FROM certificados WHERE id = ?", (cid,)).fetchone()
    if c is None:
        abort(404)
    return armazenamento().resposta(c["arquivo"], c["nome_original"])


@app.route("/colegiado/certificado/<int:cid>/avaliar", methods=["POST"])
@login_professor_requerido
def avaliar(cid):
    acao = request.form.get("acao")
    motivo = request.form.get("motivo", "").strip()
    volta = redirect(request.referrer or url_for("admin_painel"))
    if acao not in ("validar", "recusar"):
        abort(400)
    db = get_db()
    cert = db.execute(
        """SELECT c.*, a.nome FROM certificados c JOIN alunos a ON a.matricula = c.matricula
           WHERE c.id = ?""", (cid,)).fetchone()
    if cert is None:
        abort(404)

    horas_val = None
    reuniao = request.form.get("reuniao", "").strip()[:60]
    if acao == "validar":
        if not reuniao:
            flash("Informe o número da reunião do colegiado de extensão que validou o certificado.", "erro")
            return volta
        try:
            horas_val = float(request.form.get("horas_validadas", "").replace(",", "."))
        except ValueError:
            horas_val = -1
        if not (0 < horas_val <= cert["horas"]):
            flash(f"Informe a carga horária validada (maior que zero e até "
                  f"{fmt_horas(cert['horas'])}, que é o total informado pelo aluno).", "erro")
            return volta
    elif len(motivo) < 5:
        flash("Informe o motivo da recusa — ele será exibido ao aluno.", "erro")
        return volta

    with db:
        db.execute(
            """UPDATE certificados SET status = ?, motivo = ?, horas_validadas = ?, reuniao = ?,
                   avaliado_em = ?, avaliado_por = ?
               WHERE id = ?""",
            ("validado" if acao == "validar" else "recusado",
             motivo if acao == "recusar" else None, horas_val,
             reuniao if acao == "validar" else None, agora(), g.professor["id"], cid),
        )
    if acao == "validar":
        session["ultima_reuniao"] = reuniao  # pré-preenche as próximas validações

    flash(f"Certificado de {cert['nome']} {'validado' if acao == 'validar' else 'recusado'}. "
          f"O resultado já aparece no painel do aluno.", "ok")
    return volta


@app.route("/colegiado/sair")
def admin_sair():
    session.clear()
    return redirect(url_for("admin_login"))


# ---------------------------------------------------------------------------
# Coordenação de curso: certificados validados dos cursos do coordenador
# ---------------------------------------------------------------------------
@app.route("/coordenacao")
@servidor_requerido("coordenador")
def coord_painel():
    cursos = g.cursos_coordenados
    curso = request.args.get("curso", "")
    if curso and curso not in cursos:
        abort(403)
    filtro = [curso] if curso else cursos
    visao = request.args.get("visao", "certificados")
    busca = request.args.get("busca", "").strip()
    lancamento = request.args.get("lancamento", "")
    certs, resumo, contagem = [], [], {"pendentes": 0, "lancados": 0}
    if filtro:
        marcas = ",".join("?" * len(filtro))
        where = f"c.status = 'validado' AND a.curso IN ({marcas})"
        params = list(filtro)
        if busca:
            where += " AND (a.nome LIKE ? OR a.matricula LIKE ?)"
            params += [f"%{busca}%", f"%{busca}%"]
        db = get_db()
        r = db.execute(
            f"""SELECT SUM(c.lancado_em IS NULL) AS pendentes, SUM(c.lancado_em IS NOT NULL) AS lancados
                FROM certificados c JOIN alunos a ON a.matricula = c.matricula
                WHERE {where}""", params).fetchone()
        contagem = {"pendentes": r["pendentes"] or 0, "lancados": r["lancados"] or 0}
        where_certs = where + {"pendentes": " AND c.lancado_em IS NULL",
                               "lancados": " AND c.lancado_em IS NOT NULL"}.get(lancamento, "")
        certs = db.execute(
            f"""SELECT c.*, a.nome, a.curso, a.curriculo, p.nome AS avaliador,
                       pl.nome AS lancador
                FROM certificados c JOIN alunos a ON a.matricula = c.matricula
                LEFT JOIN professores p ON p.id = c.avaliado_por
                LEFT JOIN professores pl ON pl.id = c.lancado_por
                WHERE {where_certs} ORDER BY a.nome, c.avaliado_em""", params).fetchall()
        resumo = db.execute(
            f"""SELECT a.matricula, a.nome, a.curso, a.curriculo,
                       COUNT(*) AS qtd, SUM(c.horas_validadas) AS total,
                       SUM(CASE WHEN c.lancado_em IS NOT NULL THEN c.horas_validadas ELSE 0 END) AS lancadas,
                       SUM(c.lancado_em IS NULL) AS pendentes,
                       MAX(c.avaliado_em) AS ultima
                FROM certificados c JOIN alunos a ON a.matricula = c.matricula
                WHERE {where} GROUP BY a.matricula ORDER BY a.nome""", params).fetchall()
    return render_template("coord_painel.html", cursos=cursos, curso=curso, visao=visao,
                           busca=busca, certs=certs, resumo=resumo,
                           lancamento=lancamento, contagem=contagem)


@app.route("/coordenacao/certificado/<int:cid>/lancamento", methods=["POST"])
@servidor_requerido("coordenador")
def coord_lancamento(cid):
    """Marca (ou desmarca) um certificado validado como lançado no histórico do aluno."""
    db = get_db()
    c = db.execute(
        """SELECT c.id, c.lancado_em, a.curso, a.nome FROM certificados c
           JOIN alunos a ON a.matricula = c.matricula
           WHERE c.id = ? AND c.status = 'validado'""", (cid,)).fetchone()
    if c is None or c["curso"] not in g.cursos_coordenados:
        abort(404)
    desfazer = request.form.get("acao") == "desfazer"
    with db:
        if desfazer:
            db.execute("UPDATE certificados SET lancado_em = NULL, lancado_por = NULL WHERE id = ?", (cid,))
        elif c["lancado_em"] is None:
            db.execute("UPDATE certificados SET lancado_em = ?, lancado_por = ? WHERE id = ?",
                       (agora(), g.usuario["id"], cid))
    flash(f"Certificado de {c['nome']} " +
          ("voltou para pendente de lançamento." if desfazer else "marcado como lançado no histórico."), "ok")
    return redirect(request.referrer or url_for("coord_painel"))


@app.route("/coordenacao/certificado/<int:cid>/arquivo")
@servidor_requerido("coordenador")
def arquivo_coord(cid):
    c = get_db().execute(
        """SELECT c.*, a.curso FROM certificados c JOIN alunos a ON a.matricula = c.matricula
           WHERE c.id = ? AND c.status = 'validado'""", (cid,)).fetchone()
    if c is None or c["curso"] not in g.cursos_coordenados:
        abort(404)
    return armazenamento().resposta(c["arquivo"], c["nome_original"])


# ---------------------------------------------------------------------------
# Superusuário: cadastro de servidores
# ---------------------------------------------------------------------------
@app.route("/superusuario")
@servidor_requerido("superusuario")
def super_painel():
    db = get_db()
    usuarios = db.execute("SELECT * FROM professores ORDER BY ativo DESC, nome").fetchall()
    cursos_por_usuario = {}
    for r in db.execute("SELECT usuario_id, curso FROM coordenador_cursos ORDER BY curso"):
        cursos_por_usuario.setdefault(r["usuario_id"], []).append(r["curso"])
    editar = None
    if request.args.get("editar", "").isdigit():
        editar = db.execute("SELECT * FROM professores WHERE id = ?",
                            (int(request.args["editar"]),)).fetchone()
    contagem = {r["curso"]: r["n"] for r in db.execute(
        """SELECT curso, COUNT(*) AS n FROM alunos
           WHERE ativo = 1 AND cpf_hash IS NOT NULL AND matricula <> ?
           GROUP BY curso""", (ALUNO_TESTE[0],))}
    alunos_por_curso = [{"curso": c, "n": contagem.get(c, 0)} for c in CURSOS_FCTE]
    return render_template(
        "super_painel.html", usuarios=usuarios, cursos_por_usuario=cursos_por_usuario,
        editar=editar, cursos_editar=cursos_por_usuario.get(editar["id"], []) if editar else [],
        cursos=CURSOS_FCTE, alunos_por_curso=alunos_por_curso,
        total_alunos=sum(r["n"] for r in alunos_por_curso))


@app.route("/superusuario/usuario", methods=["POST"])
@servidor_requerido("superusuario")
def super_salvar_usuario():
    f = request.form
    db = get_db()
    uid = int(f["id"]) if f.get("id", "").isdigit() else None
    nome = f.get("nome", "").strip()
    email = f.get("email", "").strip().lower()
    senha = f.get("senha", "")
    papeis = {p: 1 if f.get(p) else 0 for p in PAPEIS}
    cursos = [c for c in CURSOS_FCTE if c in f.getlist("cursos")]
    volta = redirect(url_for("super_painel", editar=uid) if uid else url_for("super_painel"))

    erros = []
    if not nome:
        erros.append("Informe o nome.")
    if not email.endswith("@" + app.config["DOMINIO_PROFESSOR"]) or email.count("@") != 1:
        erros.append(f"Use um e-mail institucional @{app.config['DOMINIO_PROFESSOR']}.")
    if not any(papeis.values()):
        erros.append("Marque pelo menos um perfil de acesso.")
    if papeis["coordenador"] and not cursos:
        erros.append("Selecione o(s) curso(s) do(a) coordenador(a).")
    if uid is None and not senha:
        erros.append("Informe a senha inicial.")
    outro = db.execute("SELECT id FROM professores WHERE email = ? AND id IS NOT ?",
                       (email, uid)).fetchone()
    if outro:
        erros.append("Já existe um usuário com este e-mail.")
    if uid == g.usuario["id"] and not papeis["superusuario"]:
        erros.append("Você não pode remover o seu próprio perfil de superusuário.")
    if erros:
        for e in erros:
            flash(e, "erro")
        return volta

    with db:
        if uid is None:
            uid = db.execute(
                """INSERT INTO professores (email, nome, senha_hash, ativo, avaliador,
                       coordenador, superusuario, trocar_senha) VALUES (?, ?, ?, 1, ?, ?, ?, 1)""",
                (email, nome, generate_password_hash(senha), papeis["avaliador"],
                 papeis["coordenador"], papeis["superusuario"])).lastrowid
            msg = (f"Usuário {nome} cadastrado. Informe a senha inicial a ele; "
                   f"no primeiro acesso, o sistema vai pedir que ele a troque.")
        else:
            db.execute(
                """UPDATE professores SET email = ?, nome = ?, avaliador = ?, coordenador = ?,
                       superusuario = ? WHERE id = ?""",
                (email, nome, papeis["avaliador"], papeis["coordenador"],
                 papeis["superusuario"], uid))
            if senha:
                proprio = uid == g.usuario["id"]
                db.execute("UPDATE professores SET senha_hash = ?, trocar_senha = ? WHERE id = ?",
                           (generate_password_hash(senha), 0 if proprio else 1, uid))
            msg = f"Cadastro de {nome} atualizado" + (
                "." if not senha else
                " (senha redefinida)." if uid == g.usuario["id"] else
                " (senha redefinida; ele deverá trocá-la no próximo acesso).")
        db.execute("DELETE FROM coordenador_cursos WHERE usuario_id = ?", (uid,))
        if papeis["coordenador"]:
            db.executemany("INSERT INTO coordenador_cursos (usuario_id, curso) VALUES (?, ?)",
                           [(uid, c) for c in cursos])
    flash(msg, "ok")
    return redirect(url_for("super_painel"))


@app.route("/superusuario/usuario/<int:uid>/ativo", methods=["POST"])
@servidor_requerido("superusuario")
def super_alternar_ativo(uid):
    if uid == g.usuario["id"]:
        flash("Você não pode desativar o seu próprio acesso.", "erro")
        return redirect(url_for("super_painel"))
    db = get_db()
    u = db.execute("SELECT * FROM professores WHERE id = ?", (uid,)).fetchone()
    if u is None:
        abort(404)
    with db:
        db.execute("UPDATE professores SET ativo = ? WHERE id = ?", (0 if u["ativo"] else 1, uid))
    flash(f"Acesso de {u['nome']} {'desativado' if u['ativo'] else 'reativado'}.", "ok")
    return redirect(url_for("super_painel"))


# ---------------------------------------------------------------------------
# Superusuário: gestão de alunos
# ---------------------------------------------------------------------------
@app.route("/superusuario/alunos")
@servidor_requerido("superusuario")
def super_alunos():
    busca = request.args.get("busca", "").strip()
    curso = request.args.get("curso", "")
    situacao = request.args.get("situacao", "")
    sql = """SELECT a.*, COUNT(c.id) AS certs,
                    SUM(c.status = 'validado') AS validados,
                    SUM(c.status = 'aguardando') AS aguardando
             FROM alunos a LEFT JOIN certificados c ON c.matricula = a.matricula
             WHERE 1 = 1"""
    params = []
    if busca:
        sql += " AND (a.nome LIKE ? OR a.matricula LIKE ?)"
        params += [f"%{busca}%", f"%{busca}%"]
    if curso in CURSOS_FCTE:
        sql += " AND a.curso = ?"
        params.append(curso)
    if situacao == "pendente":
        sql += " AND a.cpf_hash IS NULL"
    elif situacao == "concluido":
        sql += " AND a.cpf_hash IS NOT NULL AND a.ativo = 1"
    elif situacao == "bloqueado":
        sql += " AND a.ativo = 0"
    sql += " GROUP BY a.matricula ORDER BY a.nome"
    alunos = get_db().execute(sql, params).fetchall()
    return render_template("super_alunos.html", alunos=alunos, busca=busca, curso=curso,
                           situacao=situacao, cursos=CURSOS_FCTE)


@app.route("/superusuario/aluno/novo", methods=["GET", "POST"])
@servidor_requerido("superusuario")
def super_novo_aluno():
    """Cadastro de aluno pelo superusuário. Com CPF, o aluno já pode entrar
    (matrícula + CPF); sem CPF, ele completa o cadastro pelo primeiro acesso."""
    dados, ativo = {"curso": ""}, 1
    if request.method == "POST":
        dados, erros = valida_dados_aluno(request.form, exigir_cpf=False)
        cpf = dados.pop("cpf")
        ativo = 1 if request.form.get("ativo") else 0
        db = get_db()
        if not erros and db.execute("SELECT 1 FROM alunos WHERE matricula = ?",
                                    (dados["matricula"],)).fetchone():
            erros.append(f"Já existe um aluno com a matrícula {dados['matricula']}.")
        if erros:
            for e in erros:
                flash(e, "erro")
        else:
            with db:
                db.execute(
                    """INSERT INTO alunos (matricula, nome, curso, curriculo, cpf_hash, ativo)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (dados["matricula"], dados["nome"], dados["curso"], dados["curriculo"],
                     hash_cpf(cpf) if cpf else None, ativo))
            flash(f"Aluno {dados['nome']} cadastrado. " + (
                "Ele já pode entrar com a matrícula e o CPF." if cpf else
                "Sem CPF informado: ele completa o cadastro pelo primeiro acesso."), "ok")
            return redirect(url_for("super_alunos", busca=dados["matricula"]))
    return render_template("super_aluno_editar.html", aluno=None, dados=dados,
                           ativo=ativo, cursos=CURSOS_FCTE)


@app.route("/superusuario/aluno/<matricula>", methods=["GET", "POST"])
@servidor_requerido("superusuario")
def super_editar_aluno(matricula):
    db = get_db()
    aluno = db.execute("SELECT * FROM alunos WHERE matricula = ?", (matricula,)).fetchone()
    if aluno is None:
        abort(404)
    if request.method == "POST":
        dados, erros = valida_dados_aluno(request.form, exigir_cpf=False)
        cpf = dados.pop("cpf")
        ativo = 1 if request.form.get("ativo") else 0
        nova = dados["matricula"]
        if nova != matricula and db.execute(
                "SELECT 1 FROM alunos WHERE matricula = ?", (nova,)).fetchone():
            erros.append(f"Já existe outro aluno com a matrícula {nova}.")
        if erros:
            for e in erros:
                flash(e, "erro")
            return render_template("super_aluno_editar.html", aluno=aluno, dados=dados,
                                   ativo=ativo, cursos=CURSOS_FCTE)
        with db:
            # Adia a checagem de chave estrangeira até o fim da transação, para
            # poder trocar a matrícula do aluno e dos certificados juntos.
            db.execute("PRAGMA defer_foreign_keys = ON")
            db.execute(
                """UPDATE alunos SET matricula = ?, nome = ?, curso = ?, curriculo = ?, ativo = ?
                   WHERE matricula = ?""",
                (nova, dados["nome"], dados["curso"], dados["curriculo"], ativo, matricula))
            if nova != matricula:
                db.execute("UPDATE certificados SET matricula = ? WHERE matricula = ?",
                           (nova, matricula))
            if cpf:
                db.execute("UPDATE alunos SET cpf_hash = ? WHERE matricula = ?",
                           (hash_cpf(cpf), nova))
            if nova != matricula or dados["curso"] != aluno["curso"]:
                renomeia_arquivos_do_aluno(db, nova)
        partes = ["Dados de " + dados["nome"] + " atualizados"]
        if nova != matricula:
            partes.append(f"matrícula alterada de {matricula} para {nova}")
        if cpf:
            partes.append("CPF atualizado")
        flash("; ".join(partes) + ".", "ok")
        return redirect(url_for("super_alunos", busca=nova))
    return render_template("super_aluno_editar.html", aluno=aluno, dados=dict(aluno),
                           ativo=aluno["ativo"], cursos=CURSOS_FCTE)


# ---------------------------------------------------------------------------
# Filtros de template
# ---------------------------------------------------------------------------
@app.template_filter("data_br")
def data_br(valor):
    if not valor:
        return ""
    try:
        if len(valor) == 10:
            return date.fromisoformat(valor).strftime("%d/%m/%Y")
        return datetime.fromisoformat(valor).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return valor


@app.template_filter("horas")
def fmt_horas(valor):
    valor = float(valor or 0)
    return (f"{valor:.0f}" if valor.is_integer() else f"{valor:.1f}".replace(".", ",")) + " h"


# ---------------------------------------------------------------------------
# Comandos de linha de comando
# ---------------------------------------------------------------------------
@app.cli.command("enviar-pdfs-para-r2")
def cmd_enviar_r2():
    """Copia para o Cloudflare R2 os PDFs que estão na pasta local (UPLOAD_FOLDER).
    Use uma vez, ao passar a usar o R2. Os arquivos locais não são apagados."""
    if not r2_configurado():
        raise click.ClickException("Defina R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY e R2_BUCKET.")
    init_db()
    local = ArmazenamentoLocal(app.config["UPLOAD_FOLDER"])
    r2 = armazenamento()
    enviados = ja_existiam = faltando = 0
    for (nome,) in get_db().execute("SELECT arquivo FROM certificados ORDER BY id"):
        if r2.existe(nome):
            ja_existiam += 1
        elif local.existe(nome):
            with open(local.pasta / nome, "rb") as f:
                r2.salvar(nome, f)
            enviados += 1
        else:
            faltando += 1
            click.echo(f"  não encontrado na pasta local: {nome}")
    click.echo(f"{enviados} PDFs enviados ao R2, {ja_existiam} já estavam lá, {faltando} não encontrados.")


@app.cli.command("testar-armazenamento")
def cmd_testar_armazenamento():
    """Grava, lê e apaga um arquivo de teste para conferir a configuração."""
    arm = armazenamento()
    nome = f".teste-{uuid.uuid4().hex}.pdf"
    try:
        arm.salvar(nome, io.BytesIO(b"%PDF-1.4 teste"))
        ok = arm.existe(nome)
        arm.apagar(nome)
    except Exception as e:
        raise click.ClickException(f"Falha no armazenamento ({arm.descricao}): {e}")
    click.echo(f"Armazenamento em uso: {arm.descricao} — " + ("funcionando." if ok else "o arquivo não foi encontrado após gravar."))


@app.cli.command("criar-professor")
@click.argument("email")
@click.argument("nome")
@click.option("--senha", prompt=True, hide_input=True, confirmation_prompt=True)
def cmd_criar_professor(email, nome, senha):
    """Credencia um professor (e-mail institucional) com senha."""
    init_db()
    email = email.strip().lower()
    if not email.endswith("@" + app.config["DOMINIO_PROFESSOR"]):
        raise click.ClickException(f"Use um e-mail institucional @{app.config['DOMINIO_PROFESSOR']}.")
    erro = senha_valida(senha)
    if erro:
        raise click.ClickException(erro)
    db = get_db()
    with db:
        db.execute(
            """INSERT INTO professores (email, nome, senha_hash, ativo, avaliador) VALUES (?, ?, ?, 1, 1)
               ON CONFLICT(email) DO UPDATE SET nome = excluded.nome,
                 senha_hash = excluded.senha_hash, ativo = 1, avaliador = 1""",
            (email, nome, generate_password_hash(senha)),
        )
    click.echo(f"Professor {nome} <{email}> credenciado.")


@app.cli.command("criar-superusuario")
@click.argument("email")
@click.argument("nome")
@click.option("--senha", prompt=True, hide_input=True, confirmation_prompt=True)
def cmd_criar_superusuario(email, nome, senha):
    """Cria (ou promove) um superusuário, que acessa a página de administração."""
    init_db()
    email = email.strip().lower()
    if not email.endswith("@" + app.config["DOMINIO_PROFESSOR"]):
        raise click.ClickException(f"Use um e-mail institucional @{app.config['DOMINIO_PROFESSOR']}.")
    if senha_valida(senha):
        raise click.ClickException(senha_valida(senha))
    db = get_db()
    with db:
        db.execute(
            """INSERT INTO professores (email, nome, senha_hash, ativo, avaliador, superusuario)
               VALUES (?, ?, ?, 1, 0, 1)
               ON CONFLICT(email) DO UPDATE SET nome = excluded.nome,
                 senha_hash = excluded.senha_hash, ativo = 1, superusuario = 1""",
            (email, nome, generate_password_hash(senha)),
        )
    click.echo(f"Superusuário {nome} <{email}> criado.")


@app.cli.command("descredenciar-professor")
@click.argument("email")
def cmd_descredenciar(email):
    """Remove o acesso de um professor (mantém o histórico de avaliações)."""
    init_db()
    db = get_db()
    with db:
        n = db.execute("UPDATE professores SET ativo = 0 WHERE email = ?",
                       (email.strip().lower(),)).rowcount
    click.echo("Professor descredenciado." if n else "E-mail não encontrado.")


if __name__ == "__main__":
    app.run(debug=True)
