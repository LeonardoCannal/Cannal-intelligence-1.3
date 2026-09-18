# Cannal — Mapeamento de médicos prescritores

Sistema de busca de médicos por região, com captação de dados de 5 fontes
públicas: Doctoralia, Sechat, Ama-me, Kaya Doc e Cannaceia. Traz nome, CRM,
especialidade, cidade, UF, endereço (quando disponível) e telefone (quando
disponível). Agora com cadastro, login e painel de administrador.

## Cadastro, login e painel admin

- Ao acessar o site, quem não estiver logado é levado pra tela de login.
- Quem não tem conta clica em "Cadastre-se" (nome, e-mail e senha).
- **Só o e-mail `leonardo@grupocannal.com` vira administrador
  automaticamente**, não importa a ordem de cadastro. Qualquer outra
  conta é usuário comum. Pra adicionar outro admin fixo, é só me pedir
  (ou editar a lista `EMAILS_ADMIN` no `auth.py`).
- Administradores veem um link "Painel Admin" no menu, com a lista de
  todo mundo que já se cadastrou (nome, e-mail, data de cadastro e último
  login).
- Pra promover outra pessoa a admin depois, ainda não tem uma tela pra
  isso — é preciso editar o banco de dados diretamente. Avise se quiser
  que eu monte essa tela também.

### Banco de dados: local (SQLite) ou externo (PostgreSQL)

O Cannal escolhe sozinho qual banco usar, sem você mudar nada no código:

- **Rodando local ou no `.exe`**: usa um arquivo `cannal.db` (SQLite) do
  lado do programa. Não precisa configurar nada.
- **Rodando no Render com um banco PostgreSQL conectado**: usa esse banco
  externo automaticamente, através da variável de ambiente
  `DATABASE_URL` que o Render preenche sozinho. Os cadastros ficam
  permanentes, mesmo quando o serviço reinicia ou é atualizado.

**Como conectar o PostgreSQL no Render (opção 1):**

1. No painel do Render, clique em "New" > "PostgreSQL". Dê um nome (ex:
   "cannal-db") e escolha o plano gratuito.
2. Espere o banco ser criado (1-2 minutos).
3. Entre no seu Web Service do Cannal (o mesmo de sempre) e vá em
   "Environment". Adicione uma variável `DATABASE_URL` com o valor da
   "Internal Database URL" que aparece na página do banco que você
   acabou de criar (o Render às vezes já sugere isso automaticamente
   como "Add from Database" — se aparecer essa opção, é só usar).
4. Salve — o Render reinicia o serviço sozinho. A partir daí, os
   cadastros vão pro banco PostgreSQL, e a tabela de usuários é criada
   automaticamente na primeira vez que o Cannal roda.

**Como conectar o Supabase (opção 2):**

1. Crie uma conta em supabase.com e um projeto novo (plano gratuito).
2. No painel do projeto, vá em "Connect" (ou "Project Settings" >
   "Database").
3. Copie a "Connection string" no modo **Transaction pooler** (não a
   "Direct connection") — isso é importante: o Cannal abre uma conexão
   nova a cada busca, e o modo pooler aguenta bem esse tipo de uso; o
   modo direto tem um limite baixo de conexões simultâneas no plano
   gratuito e pode travar rápido.
4. A connection string vem parecida com:
   `postgresql://postgres.xxxxxxxx:[SUA-SENHA]@aws-0-xxxxx.pooler.supabase.com:6543/postgres`
   Troque `[SUA-SENHA]` pela senha do banco que você definiu ao criar o
   projeto.
5. No Render, vá em "Environment" do seu Web Service e adicione essa
   string completa como `DATABASE_URL`.
6. Salve — o Render reinicia sozinho, e a tabela de usuários é criada
   automaticamente na primeira vez que o Cannal roda contra esse banco.

**Importante:** eu não tenho como testar a conexão com um PostgreSQL ou
Supabase de verdade no meu ambiente (não tenho acesso a um banco real
aqui), então revisei o código com cuidado, mas o primeiro teste de
verdade vai ser você criando uma conta no site publicado. Se der algum
erro, me manda a mensagem que aparecer que a gente ajusta.

Também é importante definir a variável de ambiente `SECRET_KEY` com um
valor fixo quando for hospedar (no Render: Settings > Environment).
Sem isso, toda vez que o servidor reiniciar, todo mundo é deslogado
automaticamente, porque uma chave nova é gerada a cada vez.

## Start Command no Render (importante pra buscas com várias especialidades)

Use este Start Command no Render, em vez de só `gunicorn app:app`:

```
gunicorn app:app --timeout 120 --workers 2 --threads 4
```

O `--timeout 120` dá mais tempo pro servidor responder buscas grandes
(muitas especialidades selecionadas de uma vez) antes de desistir. As
buscas já rodam em paralelo internamente, mas com conexões de internet
mais lentas ou sites bloqueando temporariamente, uma folga extra ajuda.

## Transformar em programa (.exe) para deixar só no PC

Se você quiser um arquivo que abre o Cannal com um duplo clique (sem precisar
abrir terminal nem digitar comandos), dá pra empacotar tudo com o PyInstaller:

1. No terminal, dentro da pasta do projeto, instale o PyInstaller:
   ```
   pip install pyinstaller
   ```
2. Gere o executável:
   ```
   pyinstaller --onefile --add-data "templates;templates" --add-data "static;static" --name Cannal app.py
   ```
   (no Mac/Linux, troque o `;` por `:` nas duas partes)
3. Espere terminar. O arquivo final aparece em `dist/Cannal.exe`.
4. Dê dois cliques em `Cannal.exe` — ele abre uma janela preta (é o servidor
   rodando) e, sozinho, abre o navegador já na tela do sistema.
5. Pra fechar o programa, é só fechar aquela janela preta.

Você pode copiar só o `Cannal.exe` pra área de trabalho ou qualquer pasta —
ele não precisa mais dos outros arquivos do projeto pra funcionar.

## Como rodar

1. Abra a pasta `catalina` no VS Code.
2. Crie um ambiente virtual (opcional, mas recomendado):
   ```
   python -m venv venv
   venv\Scripts\activate      (Windows)
   source venv/bin/activate   (Mac/Linux)
   ```
3. Instale as dependências:
   ```
   pip install -r requirements.txt
   ```
4. Rode o servidor:
   ```
   python app.py
   ```
5. Acesse no navegador: http://localhost:5000

## Como funciona

- `app.py`: servidor Flask, serve a página e expõe `/api/buscar` e `/api/exportar`.
- `auth.py`: cadastro, login, sessão e painel admin. Usa SQLite local por
  padrão, ou PostgreSQL externo automaticamente se a variável de ambiente
  `DATABASE_URL` estiver definida.
- `scraper.py`: faz a captação em cada fonte (Doctoralia, Sechat, Ama-me,
  Kaya Doc, Cannaceia) e devolve os médicos encontrados, já filtrados pela
  cidade pesquisada.
- `templates/index.html`: interface (identidade visual Cannal: fundo #343C4C
  na barra do logo, #88AD36 na barra abaixo, tipografia Montserrat, marca
  d'água do ícone verde), com filtro de especialidades, abas de Buscar e
  Histórico, tabela de resultados e exportação para Excel.
- `templates/login.html`, `cadastro.html`, `admin.html`, `erro_acesso.html`:
  telas de autenticação e o painel de usuários cadastrados.

## Confiabilidade por fonte

- **Doctoralia**: captação testada e filtrada por cidade (usa a própria URL
  do perfil do médico pra confirmar a cidade). Não expõe telefone.
- **Sechat**: captação testada com base na estrutura pública da lista de
  prescritores. É a única fonte que costuma trazer telefone e endereço
  completo diretamente.
- **Ama-me, Kaya Doc, Cannaceia**: captação best-effort/experimental. Essas
  páginas parecem carregar a lista de médicos de forma mais dinâmica (tipo
  um aplicativo dentro do site), então o scraper pode retornar poucos ou
  nenhum resultado até ser ajustado com base no que aparecer de verdade ao
  rodar. Se isso acontecer, é só avisar com o que você vê na tela (ou um
  print) que dá pra ajustar o `scraper.py`.

## Limitações conhecidas

- **CRM**: extraído por regras heurísticas (regex) em várias fontes. Pode
  vir como "Não encontrado" em alguns casos.
- **Telefone**: o Sechat é a fonte mais confiável (mostra na própria
  página). No Doctoralia, o telefone só aparece quando o médico deixou
  ele visível por trás do botão "Ver número" — o Cannal tenta captar
  esse número direto do HTML, mas se o próprio Doctoralia não expuser
  ele daquele jeito específico (varia perfil a perfil), aparece como
  "Não disponível".
- **Fontes com erro não travam a busca**: se uma fonte falhar (bloqueio
  temporário, mudança na página), as outras continuam normalmente — um
  aviso aparece na tela informando quais fontes não responderam.
- **Estrutura dos sites**: qualquer um deles pode mudar o HTML das páginas
  a qualquer momento, o que pode quebrar a captação daquela fonte
  especificamente — pode ser necessário ajustar `scraper.py`.
- **Termos de uso**: scraping automatizado pode não estar de acordo com os
  Termos de Uso de cada site. Recomendado para uso pessoal/interno, com
  volume baixo de requisições.
