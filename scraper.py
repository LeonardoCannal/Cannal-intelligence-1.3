"""
scraper.py
Faz a captação (scraping) de médicos em múltiplas fontes públicas:
Doctoralia, Sechat, Ama-me, Kaya Doc e Cannaceia.

IMPORTANTE — leia antes de usar:
- Doctoralia e Sechat foram verificados com maior confiança (a estrutura
  de dados dessas páginas foi conferida antes de escrever o código).
- Ama-me, Kaya Doc e Cannaceia usam extração best-effort/genérica, porque
  essas páginas parecem carregar a lista de médicos de forma mais dinâmica
  (tipo um aplicativo dentro do site). Isso significa que elas podem
  retornar poucos ou nenhum resultado até serem ajustadas com base no que
  aparecer de verdade quando você rodar o sistema.
- Nenhum desses sites expõe telefone de forma garantida, exceto o Sechat,
  que lista telefone e endereço completo diretamente nos dados públicos.
- Os sites podem mudar a estrutura das páginas a qualquer momento, o que
  pode quebrar este scraper.
- Fazer scraping automatizado pode não estar de acordo com os Termos de
  Uso de cada site. Use por sua conta e risco, preferencialmente para uso
  pessoal/interno e com volume baixo de requisições.
"""

import re
import unicodedata
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}

CAMPOS_PADRAO = ["nome", "crm", "cidade", "uf", "endereco", "telefone", "especialidade", "perfil_url", "fonte"]


def registro_vazio(**kwargs) -> dict:
    """Cria um registro de médico com todos os campos padrão, preenchendo o que for passado."""
    registro = {campo: "Não encontrado" for campo in CAMPOS_PADRAO}
    registro.update(kwargs)
    return registro


def slugify(texto: str) -> str:
    """Remove acentos, deixa minúsculo e troca espaços por hífen."""
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = texto.lower().strip()
    texto = re.sub(r"[^a-z0-9\s-]", "", texto)
    texto = re.sub(r"\s+", "-", texto)
    texto = re.sub(r"-+", "-", texto)
    return texto


def normalizar(texto: str) -> str:
    """Minúsculo e sem acento, mas mantendo espaços — pra comparar nomes de cidade."""
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return texto.lower().strip()


# ---------------------------------------------------------------------------
# Padrões usados por mais de uma fonte
# ---------------------------------------------------------------------------

RE_CRM_COM_PREFIXO = re.compile(
    r"CRM[\s:\-]*([A-Z]{2})?[\s\-]*n?[ºo°]?[\s:\-]*(\d{3,9})", re.IGNORECASE
)
RE_CRM_PREFIXO_NUM_UF = re.compile(
    r"CRM[\s:\-]*(\d{3,9})[\s\-]*([A-Z]{2})\b", re.IGNORECASE
)
RE_CRM_SEM_PREFIXO = re.compile(r"\b(\d{4,9})\s*[- ]?\s*([A-Z]{2})\b")

RE_TELEFONE = re.compile(r"(\(?\d{2}\)?\s?9?\d{4}[\s-]?\d{4})")

RE_EMAIL = re.compile(r"[\w\.\-+]+@[\w\.\-]+\.[a-zA-Z]{2,}")


def extrair_crm(texto: str) -> str:
    match = RE_CRM_PREFIXO_NUM_UF.search(texto)
    if match:
        numero, uf = match.groups()
        return f"CRM {uf} {numero}".strip()

    match = RE_CRM_COM_PREFIXO.search(texto)
    if match:
        uf, numero = match.groups()
        return f"CRM {uf or ''} {numero}".strip()

    match = RE_CRM_SEM_PREFIXO.search(texto)
    if match:
        numero, uf = match.groups()
        return f"CRM {uf} {numero}".strip()

    return "Não encontrado"


def extrair_telefone(texto: str) -> str:
    match = RE_TELEFONE.search(texto)
    return match.group(1).strip() if match else "Não encontrado"


def subir_ate_conter(elemento, marcador: str, max_niveis: int = 5, tamanho_maximo: int = 700):
    """
    Sobe pelos elementos pai até achar um contêiner cujo texto tenha o
    marcador buscado (ex: "Estado:", "Mapa"), sem passar de um tamanho
    máximo de texto — isso evita pegar um bloco grande demais que mistura
    o cartão de vários médicos ao mesmo tempo.
    Retorna (elemento_encontrado, texto) ou (None, "") se não achar.
    """
    atual = elemento
    for _ in range(max_niveis):
        pai = atual.find_parent(["li", "article", "div", "section"])
        if pai is None:
            break
        texto = pai.get_text(" ", strip=True)
        if marcador in texto and len(texto) <= tamanho_maximo:
            return pai, texto
        atual = pai
    return None, ""


# ---------------------------------------------------------------------------
# 1) DOCTORALIA
# ---------------------------------------------------------------------------

DOCTORALIA_BASE_URL = "https://www.doctoralia.com.br"

DOCTORALIA_PREFIXOS_IGNORADOS = (
    "clinicas", "doencas", "servicos-de-tratamento", "medicamentos",
    "blog", "perguntas-respostas", "especializacoes-medicas", "convenios",
    "registo-opcion", "social-connect", "termos-e-condicoes", "contato",
    "privacidade", "faq", "app-pacientes",
)

RE_LINK_PERFIL_DOCTORALIA = re.compile(
    r"^https://www\.doctoralia\.com\.br/([a-z0-9\-]+)/([a-z0-9\-]+)/([a-z0-9\-]+)$"
)

# Endereço completo, ex: "Avenida Trinta e Um de Março 809, Votorantim • Mapa"
# Ancorado em prefixos reais de logradouro brasileiro (com \b e exigindo
# espaço após abreviações curtas como "R." e "Av."), pra não confundir com
# pedaços de outras palavras (ex: "sala" contém "ala").
RE_ENDERECO_DOCTORALIA = re.compile(
    r"\b((?:Rua|Avenida|Alameda|Travessa|Rodovia|Estrada|Pra[çc]a|Largo|Quadra|Setor|Via"
    r"|R\.|Av\.|Al\.)\s[^•]{3,90}?)\s*•\s*Mapa",
    re.IGNORECASE,
)


def extrair_endereco_doctoralia(texto: str) -> str:
    match = RE_ENDERECO_DOCTORALIA.search(texto)
    return match.group(1).strip() if match else ""



def buscar_telefone_no_perfil(perfil_url: str):
    """
    O telefone do Doctoralia só existe na página INDIVIDUAL de cada
    médico (por trás do botão "Mostrar número de telefone") — na página
    de listagem/busca ele nunca aparece. Por isso, pra pegar o telefone
    de verdade, precisamos visitar o perfil de cada médico encontrado.
    Retorna o número (como string) ou None se não achar.
    """
    try:
        resposta = requests.get(perfil_url, headers=HEADERS, timeout=10)
        resposta.raise_for_status()
    except Exception:
        return None

    soup = BeautifulSoup(resposta.text, "html.parser")
    tel_link = soup.find("a", href=lambda h: h and h.lower().startswith("tel:"))
    if tel_link:
        return tel_link["href"].split(":", 1)[1].strip()
    return None


def buscar_medicos_doctoralia(especialidade_slug: str, especialidade_nome: str, cidade: str, uf: str = "") -> list:
    """Busca médicos no Doctoralia para uma especialidade + cidade."""
    cidade_slug = slugify(cidade)
    url = f"{DOCTORALIA_BASE_URL}/{especialidade_slug}/{cidade_slug}"

    resposta = requests.get(url, headers=HEADERS, timeout=15)
    resposta.raise_for_status()

    soup = BeautifulSoup(resposta.text, "html.parser")

    medicos = []
    vistos = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not href.startswith(DOCTORALIA_BASE_URL):
            if href.startswith("/"):
                href = DOCTORALIA_BASE_URL + href
            else:
                continue

        match = RE_LINK_PERFIL_DOCTORALIA.match(href)
        if not match:
            continue

        nome_slug, _especialidade_do_perfil, cidade_do_perfil = match.groups()
        if nome_slug in DOCTORALIA_PREFIXOS_IGNORADOS:
            continue
        if href in vistos:
            continue
        if cidade_do_perfil != cidade_slug:
            continue  # fora da cidade buscada

        nome = link.get_text(strip=True)
        if not nome:
            heading = link.find_next(["h3", "h4"])
            nome = heading.get_text(strip=True) if heading else nome_slug.replace("-", " ").title()

        card = link.find_parent(["li", "article"]) or link.find_parent("div")
        texto_card = card.get_text(" ", strip=True) if card else nome

        crm = extrair_crm(texto_card)
        if crm == "Não encontrado":
            continue

        endereco = extrair_endereco_doctoralia(texto_card)
        if not endereco:
            # o cartão imediato pode não ter o endereço — sobe mais um
            # pouco pra tentar achar um contêiner que tenha o "• Mapa"
            _, texto_maior = subir_ate_conter(link, "Mapa", max_niveis=4, tamanho_maximo=900)
            if texto_maior:
                endereco = extrair_endereco_doctoralia(texto_maior)

        cidade_encontrada = endereco.split(",")[-1].strip() if "," in endereco else cidade

        vistos.add(href)
        medicos.append(registro_vazio(
            nome=nome,
            crm=crm,
            cidade=cidade_encontrada,
            uf=uf,
            endereco=endereco or "Não encontrado",
            telefone="Não disponível",
            especialidade=especialidade_nome,
            perfil_url=href,
            fonte="Doctoralia",
        ))

    # O telefone só existe na página individual de cada médico, então
    # precisa visitar cada perfil encontrado. Faz isso em paralelo (até
    # 8 por vez) pra não multiplicar demais o tempo total da busca.
    if medicos:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futuros = {
                executor.submit(buscar_telefone_no_perfil, m["perfil_url"]): m
                for m in medicos
            }
            for futuro in as_completed(futuros):
                medico = futuros[futuro]
                try:
                    telefone_encontrado = futuro.result()
                except Exception:
                    telefone_encontrado = None
                if telefone_encontrado:
                    medico["telefone"] = telefone_encontrado

    return medicos


# ---------------------------------------------------------------------------
# 2) SECHAT
# ---------------------------------------------------------------------------

SECHAT_BASE_URL = "https://sechat.com.br/medicos"


def buscar_medicos_sechat(especialidade_id: str, especialidade_nome: str, cidade: str, uf: str = "") -> list:
    """
    Busca médicos no Sechat (lista de prescritores de cannabis medicinal).
    Filtra pelos que atendem na cidade buscada.
    """
    if not especialidade_id:
        return []

    url = f"{SECHAT_BASE_URL}?specialty={especialidade_id}"
    resposta = requests.get(url, headers=HEADERS, timeout=15)
    resposta.raise_for_status()

    soup = BeautifulSoup(resposta.text, "html.parser")

    medicos = []
    vistos = set()
    cidade_norm = normalizar(cidade)

    # Âncora em cada e-mail encontrado na página (texto ou link mailto:)
    candidatos = []
    for a in soup.find_all("a", href=True):
        if a["href"].startswith("mailto:"):
            candidatos.append(a)

    if not candidatos:
        # fallback: procura o padrão de e-mail em qualquer texto da página
        texto_completo = soup.get_text(" ", strip=True)
        for m in RE_EMAIL.finditer(texto_completo):
            candidatos.append(m.group(0))

    for candidato in candidatos:
        if hasattr(candidato, "get_text"):
            email = candidato["href"].replace("mailto:", "").strip()
            card = candidato.find_parent(["li", "article", "div"])
            texto_card = card.get_text(" · ", strip=True) if card else email

            # se o cartão imediato não tem "Estado:", sobe mais um pouco
            if "Estado:" not in texto_card:
                card_maior, texto_maior = subir_ate_conter(candidato, "Estado:", max_niveis=5, tamanho_maximo=600)
                if texto_maior:
                    card, texto_card = card_maior, texto_maior
        else:
            email = candidato
            texto_card = email  # sem estrutura, pouca informação disponível

        if email in vistos:
            continue

        if cidade_norm not in normalizar(texto_card):
            continue  # não é da cidade buscada

        match_cidade = re.search(r"Cidade:\s*([^·]+)", texto_card)
        cidade_encontrada = match_cidade.group(1).strip() if match_cidade else cidade

        match_uf = re.search(r"Estado:\s*([A-Z]{2})", texto_card)
        uf_encontrada = match_uf.group(1) if match_uf else uf

        # Endereço vem logo depois do CEP, antes do próximo "·"
        match_endereco = re.search(r"CEP:\s*[\d\-\.]+\s*·\s*([^·]+)", texto_card)
        endereco = match_endereco.group(1).strip() if match_endereco else "Não encontrado"

        # Telefone: procura especificamente o trecho entre o e-mail e "Estado:",
        # em vez de qualquer número parecido em todo o cartão (evita confundir
        # com CEP ou registro profissional).
        telefone = "Não encontrado"
        trecho_pos_email = texto_card.split(email, 1)
        if len(trecho_pos_email) == 2:
            trecho_ate_estado = trecho_pos_email[1].split("Estado:", 1)[0]
            telefone = extrair_telefone(trecho_ate_estado)
        if telefone == "Não encontrado":
            telefone = extrair_telefone(texto_card)  # fallback

        crm = extrair_crm(texto_card)

        heading = candidato.find_previous(["h2", "h3", "h4"]) if hasattr(candidato, "find_previous") else None
        nome = heading.get_text(strip=True) if heading else email.split("@")[0].replace(".", " ").title()

        vistos.add(email)
        medicos.append(registro_vazio(
            nome=nome,
            crm=crm,
            cidade=cidade_encontrada,
            uf=uf_encontrada,
            endereco=endereco,
            telefone=telefone,
            especialidade=especialidade_nome,
            perfil_url=url,
            fonte="Sechat",
        ))

    return medicos


# ---------------------------------------------------------------------------
# 3) FONTES GENÉRICAS (Ama-me, Kaya Doc, Cannaceia)
# Extração best-effort: essas páginas carregam a lista de forma mais
# dinâmica, então este scraper tenta achar cartões de médico ancorados em
# e-mail/telefone visíveis na página e filtra pelos que citam a cidade
# buscada. Pode retornar poucos ou nenhum resultado dependendo de como a
# página realmente carrega os dados no navegador.
# ---------------------------------------------------------------------------

FONTES_GENERICAS = {
    "amame": {"nome": "Ama-me", "url": "https://amame.org.br/apoio-juridico/lista-de-prescritores/"},
    "kayadoc": {"nome": "Kaya Doc", "url": "https://kayadoc.com/prescritores/"},
    "cannaceia": {"nome": "Cannaceia", "url": "https://cannaceia.com/profissionais-da-saude"},
}


def buscar_medicos_fonte_generica(chave_fonte: str, especialidade_nome: str, cidade: str, uf: str = "") -> list:
    fonte = FONTES_GENERICAS[chave_fonte]

    try:
        resposta = requests.get(fonte["url"], headers=HEADERS, timeout=15)
        resposta.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(resposta.text, "html.parser")
    cidade_norm = normalizar(cidade)

    medicos = []
    vistos = set()

    # Âncora em qualquer link de e-mail (mailto:) ou telefone (tel:) visível
    candidatos = soup.find_all("a", href=True)
    for a in candidatos:
        href = a["href"]
        eh_email = href.startswith("mailto:")
        eh_tel = href.startswith("tel:")
        if not (eh_email or eh_tel):
            continue

        identificador = href.replace("mailto:", "").replace("tel:", "").strip()
        if identificador in vistos:
            continue

        card = a.find_parent(["li", "article", "div"])
        texto_card = card.get_text(" ", strip=True) if card else identificador

        if cidade_norm not in normalizar(texto_card):
            continue

        heading = a.find_previous(["h2", "h3", "h4"])
        nome = heading.get_text(strip=True) if heading else identificador

        crm = extrair_crm(texto_card)
        telefone = identificador if eh_tel else extrair_telefone(texto_card)

        vistos.add(identificador)
        medicos.append(registro_vazio(
            nome=nome,
            crm=crm,
            cidade=cidade,
            uf=uf,
            endereco="Não encontrado",
            telefone=telefone,
            especialidade=especialidade_nome,
            perfil_url=fonte["url"],
            fonte=fonte["nome"],
        ))

    return medicos
