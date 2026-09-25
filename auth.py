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
from datetime import datetime, timedelta
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
        sql_buscas_log = """
            CREATE TABLE IF NOT EXISTS buscas_log (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
                criado_em TEXT NOT NULL
            )
        """
        sql_solicitacoes_senha = """
            CREATE TABLE IF NOT EXISTS solicitacoes_senha (
                id SERIAL PRIMARY KEY,
                cpf TEXT NOT NULL,
                nome TEXT,
                email TEXT,
                atendido BOOLEAN NOT NULL DEFAULT FALSE,
                criado_em TEXT NOT NULL
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
        sql_buscas_log = """
            CREATE TABLE IF NOT EXISTS buscas_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
                criado_em TEXT NOT NULL
            )
        """
        sql_solicitacoes_senha = """
            CREATE TABLE IF NOT EXISTS solicitacoes_senha (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cpf TEXT NOT NULL,
                nome TEXT,
                email TEXT,
                atendido INTEGER NOT NULL DEFAULT 0,
                criado_em TEXT NOT NULL
            )
        """

    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute(sql_usuarios)
        cursor.execute(sql_painel)
        cursor.execute(sql_buscas_log)
        cursor.execute(sql_solicitacoes_senha)

    # Bancos criados antes dessa versão não têm essa coluna — adiciona sem
    # quebrar se ela já existir (mesmo problema que já pegou o "cpf" antes).
    _adicionar_coluna_se_faltar("painel_medicos", "visitado_em", "TEXT")

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


def _adicionar_coluna_se_faltar(tabela: str, coluna: str, tipo_sql: str):
    """
    Adiciona uma coluna nova numa tabela que pode já existir de uma versão
    anterior do banco (sem essa coluna). Roda numa conexão própria e
    ignora silenciosamente o erro de "coluna já existe" — assim, se a
    coluna já foi criada, não quebra nada.
    """
    conn = _get_conn_bruta()
    try:
        cursor = conn.cursor()
        cursor.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo_sql}")
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


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
        if novo_status == "visitado":
            agora = datetime.utcnow().isoformat()
            cursor.execute(
                _q("UPDATE painel_medicos SET status = ?, visitado_em = ? WHERE id = ? AND usuario_id = ?"),
                (novo_status, agora, entrada_id, usuario_id),
            )
        else:
            cursor.execute(
                _q("UPDATE painel_medicos SET status = ?, visitado_em = NULL WHERE id = ? AND usuario_id = ?"),
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


# ---------------------------------------------------------------------------
# "Esqueci minha senha" — vira notificação pro admin, sem envio automático
# ---------------------------------------------------------------------------

_ATENDIDO = True if USANDO_POSTGRES else 1
_NAO_ATENDIDO = False if USANDO_POSTGRES else 0


def registrar_solicitacao_senha(cpf: str):
    """Registra o pedido de redefinição de senha pra aparecer como
    notificação no painel admin. Tenta casar com um usuário existente
    pelo CPF pra já trazer nome/e-mail junto."""
    cpf_limpo = limpar_cpf(cpf)
    usuario = buscar_usuario_por_cpf(cpf_limpo) if cpf_limpo else None
    agora = datetime.utcnow().isoformat()

    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q("""INSERT INTO solicitacoes_senha (cpf, nome, email, atendido, criado_em)
                  VALUES (?, ?, ?, ?, ?)"""),
            (
                cpf_limpo,
                usuario["nome"] if usuario else None,
                usuario["email"] if usuario else None,
                _NAO_ATENDIDO,
                agora,
            ),
        )


def listar_solicitacoes_senha_pendentes():
    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q("SELECT * FROM solicitacoes_senha WHERE atendido = ? ORDER BY criado_em DESC"),
            (_NAO_ATENDIDO,),
        )
        return cursor.fetchall()


def contar_solicitacoes_senha_pendentes() -> int:
    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q("SELECT COUNT(*) AS total FROM solicitacoes_senha WHERE atendido = ?"),
            (_NAO_ATENDIDO,),
        )
        return cursor.fetchone()["total"]


def marcar_solicitacao_atendida(solicitacao_id: int) -> bool:
    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q("UPDATE solicitacoes_senha SET atendido = ? WHERE id = ?"),
            (_ATENDIDO, solicitacao_id),
        )
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Log de buscas (pra estatística do dashboard)
# ---------------------------------------------------------------------------

def registrar_busca(usuario_id: int):
    agora = datetime.utcnow().isoformat()
    with _conexao() as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q("INSERT INTO buscas_log (usuario_id, criado_em) VALUES (?, ?)"),
            (usuario_id, agora),
        )


# ---------------------------------------------------------------------------
# Dashboard do admin
# ---------------------------------------------------------------------------

_MESES_ABREV = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]


def _inicio_do_mes(dt):
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _mes_anterior(dt):
    primeiro = _inicio_do_mes(dt)
    return _inicio_do_mes(primeiro - timedelta(days=1))


def estatisticas_dashboard():
    """Junta todos os números do Dashboard do admin numa única consulta ao
    banco (várias queries agregadas, mas uma conexão só)."""
    agora = datetime.utcnow()
    hoje_str = agora.strftime("%Y-%m-%d")
    d7_str = (agora - timedelta(days=7)).isoformat()
    d30_str = (agora - timedelta(days=30)).isoformat()

    inicio_mes_atual = _inicio_do_mes(agora)
    inicio_mes_anterior = _mes_anterior(agora)
    inicio_mes_atual_str = inicio_mes_atual.isoformat()
    inicio_mes_anterior_str = inicio_mes_anterior.isoformat()

    # limites dos últimos 6 meses (do mais antigo pro atual)
    inicios_meses = [inicio_mes_atual]
    cursor_mes = inicio_mes_atual
    for _ in range(5):
        cursor_mes = _mes_anterior(cursor_mes)
        inicios_meses.append(cursor_mes)
    inicios_meses.reverse()
    fins_meses = inicios_meses[1:] + [agora]

    with _conexao() as conn:
        cursor = conn.cursor()

        def contar(sql, params=()):
            cursor.execute(_q(sql), params)
            return cursor.fetchone()["total"]

        prospectados = contar("SELECT COUNT(*) AS total FROM painel_medicos WHERE status = 'prospectado'")
        visitados = contar("SELECT COUNT(*) AS total FROM painel_medicos WHERE status = 'visitado'")
        usuarios_total = contar("SELECT COUNT(*) AS total FROM usuarios")

        medicos_base = contar("SELECT COUNT(DISTINCT chave_medico) AS total FROM painel_medicos")
        medicos_semana = contar(
            "SELECT COUNT(DISTINCT chave_medico) AS total FROM painel_medicos WHERE adicionado_em >= ?", (d7_str,)
        )
        medicos_mes = contar(
            "SELECT COUNT(DISTINCT chave_medico) AS total FROM painel_medicos WHERE adicionado_em >= ?", (d30_str,)
        )

        visitados_mes_atual = contar(
            "SELECT COUNT(*) AS total FROM painel_medicos WHERE status = 'visitado' AND visitado_em >= ?",
            (inicio_mes_atual_str,),
        )
        visitados_mes_anterior = contar(
            "SELECT COUNT(*) AS total FROM painel_medicos WHERE status = 'visitado' AND visitado_em >= ? AND visitado_em < ?",
            (inicio_mes_anterior_str, inicio_mes_atual_str),
        )
        if visitados_mes_anterior > 0:
            variacao_visitados_pct = round((visitados_mes_atual - visitados_mes_anterior) / visitados_mes_anterior * 100)
        else:
            variacao_visitados_pct = 100 if visitados_mes_atual > 0 else 0

        buscas_hoje = contar("SELECT COUNT(*) AS total FROM buscas_log WHERE criado_em >= ?", (hoje_str,))
        buscas_7d = contar("SELECT COUNT(*) AS total FROM buscas_log WHERE criado_em >= ?", (d7_str,))
        buscas_30d = contar("SELECT COUNT(*) AS total FROM buscas_log WHERE criado_em >= ?", (d30_str,))

        ativos_hoje = contar("SELECT COUNT(*) AS total FROM usuarios WHERE ultimo_login >= ?", (hoje_str,))
        ativos_7d = contar("SELECT COUNT(*) AS total FROM usuarios WHERE ultimo_login >= ?", (d7_str,))
        ativos_30d = contar("SELECT COUNT(*) AS total FROM usuarios WHERE ultimo_login >= ?", (d30_str,))

        cursor.execute(_q("""
            SELECT especialidade, COUNT(*) AS total
            FROM painel_medicos
            WHERE especialidade IS NOT NULL AND especialidade != ''
            GROUP BY especialidade
            ORDER BY total DESC
            LIMIT 8
        """))
        top_especialidades = [dict(r) for r in cursor.fetchall()]

        cursor.execute(_q("""
            SELECT uf, COUNT(*) AS total
            FROM painel_medicos
            WHERE uf IS NOT NULL AND uf != ''
            GROUP BY uf
            ORDER BY total DESC
        """))
        por_estado = [dict(r) for r in cursor.fetchall()]

        cursor.execute(_q("""
            SELECT cidade, uf, COUNT(*) AS total
            FROM painel_medicos
            WHERE cidade IS NOT NULL AND cidade != ''
            GROUP BY cidade, uf
            ORDER BY total DESC
            LIMIT 10
        """))
        por_cidade = [dict(r) for r in cursor.fetchall()]

        serie_meses = []
        for inicio, fim in zip(inicios_meses, fins_meses):
            inicio_str = inicio.isoformat()
            fim_str = fim.isoformat()
            novos = contar(
                "SELECT COUNT(DISTINCT chave_medico) AS total FROM painel_medicos WHERE adicionado_em >= ? AND adicionado_em < ?",
                (inicio_str, fim_str),
            )
            visitados_no_mes = contar(
                "SELECT COUNT(*) AS total FROM painel_medicos WHERE status = 'visitado' AND visitado_em >= ? AND visitado_em < ?",
                (inicio_str, fim_str),
            )
            serie_meses.append({
                "label": f"{_MESES_ABREV[inicio.month - 1]}/{str(inicio.year)[2:]}",
                "novos": novos,
                "visitados": visitados_no_mes,
            })

    return {
        "prospectados": prospectados,
        "visitados": visitados,
        "usuarios_total": usuarios_total,
        "medicos_base": medicos_base,
        "medicos_semana": medicos_semana,
        "medicos_mes": medicos_mes,
        "visitados_mes_atual": visitados_mes_atual,
        "visitados_mes_anterior": visitados_mes_anterior,
        "variacao_visitados_pct": variacao_visitados_pct,
        "buscas_hoje": buscas_hoje,
        "buscas_7d": buscas_7d,
        "buscas_30d": buscas_30d,
        "ativos_hoje": ativos_hoje,
        "ativos_7d": ativos_7d,
        "ativos_30d": ativos_30d,
        "top_especialidades": top_especialidades,
        "por_estado": por_estado,
        "por_cidade": por_cidade,
        "serie_meses": serie_meses,
    }
