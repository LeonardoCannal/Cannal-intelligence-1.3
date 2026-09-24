"""
auth.py
Cadastro, login, controle de usuários e Painel Médico do Cannal.

- Usa a sessão nativa do Flask pra controlar quem está logado.
- Senhas são guardadas com hash (nunca em texto puro), usando o
  werkzeug.security que já vem junto com o Flask.
- O e-mail leonardo@grupocannal.com vira administrador automaticamente
  ao se cadastrar (veja EMAILS_ADMIN mais abaixo). Todo mundo que se
  cadastra com outro e-mail é usuário comum. Para adicionar outro admin
  fixo, basta incluir o e-mail no conjunto EMAILS_ADMIN.

BANCO DE DADOS — SQLite local OU PostgreSQL externo:
- Se a variável de ambiente DATABASE_URL estiver definida (é o que o
  Render preenche sozinho quando você conecta um banco PostgreSQL ao
  serviço, ou a connection string do Supabase), o Cannal usa esse banco
  externo — os cadastros ficam permanentes mesmo quando o serviço
  reinicia.
- Se DATABASE_URL não estiver definida (rodando local ou no .exe), o
  Cannal usa um arquivo `cannal.db` (SQLite) do lado do programa.
- Você não precisa mudar nada no código pra trocar entre os dois — é só
  a variável de ambiente estar definida ou não.

PAINEL MÉDICO:
- Cada usuário tem sua própria lista de médicos "prospectados" e
  "visitados" — é o controle pessoal de quem ele já buscou e quem já
  visitou. A "chave" de cada médico é o CRM (quando existe) ou o nome +
  cidade (quando não tem CRM identificado), pra saber se um médico que
  aparece de novo numa busca já está no painel de alguém.
"""

import os
import re
import sqlite3
from contextlib import contextmanager
from functools import wraps
from datetime import datetime
from flask import session, redirect, url_for, render_template
from werkzeug.security import generate_password_hash, check_password_hash

DATABASE_URL = os.environ.get("DATABASE_URL")
USANDO_POSTGRES = bool(DATABASE_URL)

if USANDO_POSTGRES:
    import psycopg2
    import psycopg2.extras
    ErroIntegridade = psycopg2.IntegrityError
else:
    ErroIntegridade = sqlite3.IntegrityError

_SQLITE_PATH = None


def inicializar_auth(app, sqlite_path):
    """
    Chama isso uma vez, logo depois de criar o app Flask.
    `sqlite_path` só é usado quando NÃO tem DATABASE_URL definida.
    """
    global _SQLITE_PATH
    _SQLITE_PATH = sqlite_path

    if USANDO_POSTGRES:
        sql_usuarios = """
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                nome TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                cpf TEXT UNIQUE NOT NULL,
                senha_hash TEXT NOT NULL,
                is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                criado_em TEXT NOT NULL,
                ultimo_login TEXT
            )
        """
        sql_painel = """
            CREATE TABLE IF NOT EXISTS painel_medicos (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
                chave_medico TEXT NOT NULL,
                nome TEXT NOT NULL,
                crm TEXT,
                especialidade TEXT,
                cidade TEXT,
                uf TEXT,
                endereco TEXT,
                telefone TEXT,
                status TEXT NOT NULL DEFAULT 'prospectado',
                adicionado_em TEXT NOT NULL,
                UNIQUE(usuario_id, chave_medico)
            )
        """
    else:
        sql_usuarios = """
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                cpf TEXT UNIQUE NOT NULL,
                senha_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                criado_em TEXT NOT NULL,
                ultimo_login TEXT
            )
        """
        sql_painel = """
            CREATE TABLE IF NOT EXISTS painel_medicos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
                chave_medico TEXT NOT NULL,
                nome TEXT NOT NULL,
                crm TEXT,
                especialidade TEXT,
                cidade TEXT,
                uf TEXT,
                endereco TEXT,
                telefone TEXT,
                status TEXT NOT NULL DEFAULT 'prospectado',
                adicionado_em TEXT NOT NULL,
                UNIQUE(usuario_id, chave_medico)
            )
        """

    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute(sql_usuarios)
        cursor.execute(sql_painel)

    @app.context_processor
    def injetar_usuario_logado():
        return {"current_user": usuario_logado()}


def _get_conn_bruta():
    if USANDO_POSTGRES:
        kwargs = {}
        if "sslmode" not in DATABASE_URL:
            kwargs["sslmode"] = "require"  # exigido pelo Postgres gerenciado do Render/Supabase
        return psycopg2.connect(
            DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
            **kwargs,
        )
    conn = sqlite3.connect(_SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _conexao():
    """Abre uma conexão, garante commit/rollback certo e sempre fecha no final."""
    conn = _get_conn_bruta()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _q(sql: str) -> str:
    """Troca os placeholders '?' por '%s' quando o banco é PostgreSQL."""
    return sql.replace("?", "%s") if USANDO_POSTGRES else sql


# ---------------------------------------------------------------------------
# CPF
# ---------------------------------------------------------------------------

def limpar_cpf(cpf: str) -> str:
    return re.sub(r"\D", "", cpf or "")


def cpf_valido(cpf: str) -> bool:
    """Valida o CPF pelo algoritmo oficial dos dígitos verificadores."""
    cpf = limpar_cpf(cpf)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False

    soma = sum(int(cpf[i]) * (10 - i) for i in range(9))
    resto = (soma * 10) % 11
    dv1 = 0 if resto == 10 else resto
    if dv1 != int(cpf[9]):
        return False

    soma = sum(int(cpf[i]) * (11 - i) for i in range(10))
    resto = (soma * 10) % 11
    dv2 = 0 if resto == 10 else resto
    return dv2 == int(cpf[10])


def formatar_cpf(cpf: str) -> str:
    cpf = limpar_cpf(cpf)
    if len(cpf) != 11:
        return cpf
    return f"{cpf[0:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:11]}"


# ---------------------------------------------------------------------------
# Usuários
# ---------------------------------------------------------------------------

def contar_usuarios() -> int:
    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS total FROM usuarios")
        return cursor.fetchone()["total"]


def buscar_usuario_por_email(email: str):
    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute(_q("SELECT * FROM usuarios WHERE email = ?"), (email,))
        return cursor.fetchone()


def buscar_usuario_por_cpf(cpf: str):
    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute(_q("SELECT * FROM usuarios WHERE cpf = ?"), (limpar_cpf(cpf),))
        return cursor.fetchone()


def buscar_usuario_por_id(user_id: int):
    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute(_q("SELECT * FROM usuarios WHERE id = ?"), (user_id,))
        return cursor.fetchone()


# Só esse(s) e-mail(is) recebem admin automaticamente ao se cadastrar.
# Pra adicionar outro admin fixo depois, é só colocar o e-mail aqui.
EMAILS_ADMIN = {"leonardo@grupocannal.com"}


def criar_usuario(nome: str, email: str, cpf: str, senha: str) -> int:
    is_admin = email.strip().lower() in EMAILS_ADMIN
    cpf_limpo = limpar_cpf(cpf)
    senha_hash = generate_password_hash(senha)
    agora = datetime.utcnow().isoformat()

    with _conexao() as conn:
        cursor = conn.cursor()
        if USANDO_POSTGRES:
            cursor.execute(
                """INSERT INTO usuarios (nome, email, cpf, senha_hash, is_admin, criado_em)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (nome, email, cpf_limpo, senha_hash, is_admin, agora),
            )
            return cursor.fetchone()["id"]
        else:
            cursor.execute(
                """INSERT INTO usuarios (nome, email, cpf, senha_hash, is_admin, criado_em)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (nome, email, cpf_limpo, senha_hash, int(is_admin), agora),
            )
            return cursor.lastrowid


def checar_senha(usuario_row, senha: str) -> bool:
    return check_password_hash(usuario_row["senha_hash"], senha)


def atualizar_ultimo_login(user_id: int):
    agora = datetime.utcnow().isoformat()
    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute(_q("UPDATE usuarios SET ultimo_login = ? WHERE id = ?"), (agora, user_id))


def listar_usuarios():
    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM usuarios ORDER BY criado_em DESC")
        return cursor.fetchall()


def usuario_logado():
    """Retorna a linha do usuário logado (via sessão) ou None."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    return buscar_usuario_por_id(user_id)


def fazer_login(user_id: int):
    session["user_id"] = user_id
    session.permanent = True


def fazer_logout():
    session.pop("user_id", None)


def login_required(f):
    @wraps(f)
    def decorador(*args, **kwargs):
        if not usuario_logado():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorador


def admin_required(f):
    @wraps(f)
    def decorador(*args, **kwargs):
        usuario = usuario_logado()
        if not usuario:
            return redirect(url_for("login"))
        if not usuario["is_admin"]:
            return render_template("erro_acesso.html"), 403
        return f(*args, **kwargs)
    return decorador


# ---------------------------------------------------------------------------
# Painel Médico
# ---------------------------------------------------------------------------

def computar_chave_medico(medico: dict) -> str:
    """
    Identifica um médico de forma estável entre buscas diferentes: usa o
    CRM quando dá pra confiar nele, ou nome+cidade como alternativa.
    """
    crm = (medico.get("crm") or "").strip()
    if crm and crm.lower() != "não encontrado":
        return "crm:" + re.sub(r"\s+", "", crm).lower()

    nome_norm = re.sub(r"\s+", " ", (medico.get("nome") or "").strip()).lower()
    cidade_norm = re.sub(r"\s+", " ", (medico.get("cidade") or "").strip()).lower()
    return f"nomecidade:{nome_norm}|{cidade_norm}"


def adicionar_ao_painel(usuario_id: int, medico: dict):
    """
    Adiciona um médico ao painel do usuário, como 'prospectado'.
    Cada médico só pode estar no painel de UM usuário por vez: se ele já
    está no painel de outra pessoa, a inclusão é bloqueada com uma
    mensagem específica. Retorna (ok, mensagem).
    """
    chave = computar_chave_medico(medico)
    agora = datetime.utcnow().isoformat()

    with _conexao() as conn:
        cursor = conn.cursor()

        # Checa se esse médico já está em ALGUM painel (de qualquer usuário).
        cursor.execute(
            _q("SELECT usuario_id FROM painel_medicos WHERE chave_medico = ? LIMIT 1"),
            (chave,),
        )
        existente = cursor.fetchone()
        if existente:
            if existente["usuario_id"] == usuario_id:
                return False, "Esse médico já está no seu painel."
            return False, "Médico cadastrado em outro painel médico."

        try:
            cursor.execute(
                _q("""INSERT INTO painel_medicos
                      (usuario_id, chave_medico, nome, crm, especialidade, cidade, uf, endereco, telefone, status, adicionado_em)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prospectado', ?)"""),
                (
                    usuario_id, chave,
                    medico.get("nome", ""), medico.get("crm", ""),
                    medico.get("especialidade", ""), medico.get("cidade", ""),
                    medico.get("uf", ""), medico.get("endereco", ""),
                    medico.get("telefone", ""), agora,
                ),
            )
        except ErroIntegridade:
            return False, "Esse médico já está no seu painel."
    return True, "Adicionado ao painel médico."


def mover_no_painel(usuario_id: int, entrada_id: int, novo_status: str) -> bool:
    if novo_status not in ("prospectado", "visitado"):
        return False
    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q("UPDATE painel_medicos SET status = ? WHERE id = ? AND usuario_id = ?"),
            (novo_status, entrada_id, usuario_id),
        )
        return cursor.rowcount > 0


def listar_painel_usuario(usuario_id: int):
    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q("SELECT * FROM painel_medicos WHERE usuario_id = ? ORDER BY adicionado_em DESC"),
            (usuario_id,),
        )
        return cursor.fetchall()


def chaves_no_painel_usuario(usuario_id: int) -> set:
    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q("SELECT chave_medico FROM painel_medicos WHERE usuario_id = ?"),
            (usuario_id,),
        )
        return {row["chave_medico"] for row in cursor.fetchall()}


def listar_usuarios_com_contagem_painel():
    """Pra tela de admin: cada usuário com quantos médicos tem no painel dele."""
    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.nome, u.email, u.cpf, u.criado_em, u.ultimo_login, u.is_admin,
                   COUNT(p.id) AS total_painel
            FROM usuarios u
            LEFT JOIN painel_medicos p ON p.usuario_id = u.id
            GROUP BY u.id, u.nome, u.email, u.cpf, u.criado_em, u.ultimo_login, u.is_admin
            ORDER BY u.criado_em DESC
        """)
        return cursor.fetchall()
