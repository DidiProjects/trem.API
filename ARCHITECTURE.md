# trem.API — Arquitetura e Plano de Implementação

## Visão Geral

trem.API é uma plataforma de processamento de arquivos com identidade própria, autenticação segura, permissões por aplicação e capacidade de integração com serviços externos na mesma rede Docker.

---

## Arquitetura em Camadas

```
┌──────────────────────────────────────────────────────────────┐
│  API Layer  — routers, schemas de entrada/saída, middleware  │
├──────────────────────────────────────────────────────────────┤
│  Service Layer  — regras de negócio, orquestração            │
├──────────────────────────────────────────────────────────────┤
│  Repository Layer  — abstração de acesso a dados             │
├──────────────────────────────────────────────────────────────┤
│  Infrastructure  — DB, HTTP externo, email, cache            │
└──────────────────────────────────────────────────────────────┘
```

Cada camada se comunica apenas com a camada imediatamente abaixo, via **interfaces abstratas**. A injeção de dependência do FastAPI (`Depends`) conecta tudo sem acoplamento direto.

---

## Estrutura de Diretórios

```
app/
├── api/
│   ├── v1/
│   │   ├── routers/
│   │   │   ├── auth.py          # login, refresh, change-password, logout
│   │   │   ├── users.py         # admin: criar, habilitar, enviar senha provisória
│   │   │   ├── pdf.py           # (migrado de routers/pdfRoute.py)
│   │   │   ├── video.py
│   │   │   ├── audio.py
│   │   │   ├── image.py
│   │   │   └── support.py
│   │   └── dependencies.py      # get_current_user, require_permission, get_db
│   └── middleware/
│       ├── body_size_limit.py   # 413 em JSON por prefixo; o proxy não limita corpo
│       ├── rate_limit.py
│       └── request_logger.py
│
├── core/
│   ├── config.py                # Settings via pydantic-settings
│   ├── security.py              # JWT RS256 + Argon2id + geração de senhas
│   ├── exceptions.py            # Exceções de domínio mapeadas para HTTP
│   └── interfaces/
│       ├── i_user_repository.py
│       └── i_email_client.py
│
├── domain/
│   ├── entities/
│   │   ├── user.py              # Entidade User (pura, sem ORM)
│   │   └── permission.py        # Application, Permission, TremPermission
│   └── schemas/
│       ├── auth.py              # LoginRequest, TokenResponse
│       ├── user.py              # UserCreate, UserResponse
│       └── common.py            # Paginação, envelopes de resposta
│
├── services/
│   ├── auth_service.py          # login, refresh, troca de senha, logout
│   ├── user_service.py          # CRUD usuários, envio de senha provisória
│   ├── file/
│   │   ├── pdf_service.py       # (mantido de services/pdfService.py)
│   │   ├── video_service.py
│   │   ├── audio_service.py
│   │   └── image_service.py
│   └── email_service.py         # (mantido para suporte/feedback)
│
├── repositories/
│   ├── base.py                  # BaseRepository genérico com CRUD
│   └── user_repository.py       # Implementação concreta (SQLAlchemy async)
│
├── infrastructure/
│   ├── database/
│   │   ├── connection.py        # AsyncEngine, session factory
│   │   └── models.py            # Modelos ORM SQLAlchemy (UserModel, etc.)
│   ├── http/
│   │   ├── airline_client.py    # Cliente HTTP p/ API aérea na rede Docker
│   │   └── llm_client.py        # Cliente HTTP p/ o modelo (trem-llm)
│   └── email/
│       └── smtp_client.py       # Cliente SMTP reutilizável
│
├── utils/
│   ├── filename.py
│   ├── file_validator.py
│   └── pagination.py
│
├── static/
└── main.py

alembic/
├── env.py
├── script.py.mako
└── versions/
    └── 001_initial_schema.py

scripts/
└── generate_keys.py             # Geração do par RS256 (PEM)
```

---

## Banco de Dados PostgreSQL

### Schema

```sql
-- Interfaces que autenticam pela trem.API; o slug é o audience do token
CREATE TABLE applications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        VARCHAR(50) UNIQUE NOT NULL,
    name        VARCHAR(100) NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Permissões de cada aplicação; a chave pública é "<slug>:<name>", ex: trem:ai
CREATE TABLE permissions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    name           VARCHAR(50) NOT NULL,
    description    TEXT,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (application_id, name)
);

-- Usuários
CREATE TABLE users (
    id                           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username                     VARCHAR(100) UNIQUE NOT NULL,
    email                        VARCHAR(255) UNIQUE,
    password_hash                TEXT NOT NULL,
    status                       VARCHAR(20) NOT NULL DEFAULT 'blocked',
    must_change_password         BOOLEAN NOT NULL DEFAULT TRUE,
    provisional_password_sent_at TIMESTAMPTZ,
    created_at                   TIMESTAMPTZ DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ DEFAULT NOW(),
    last_login_at                TIMESTAMPTZ
);

-- Permissões de cada usuário e de cada chave: um conjunto
CREATE TABLE user_permissions (
    user_id       UUID REFERENCES users(id) ON DELETE CASCADE,
    permission_id UUID REFERENCES permissions(id) ON DELETE CASCADE,
    granted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    granted_by    UUID REFERENCES users(id) ON DELETE SET NULL,
    PRIMARY KEY (user_id, permission_id)
);
CREATE TABLE api_client_permissions (
    client_id     UUID REFERENCES api_clients(id) ON DELETE CASCADE,
    permission_id UUID REFERENCES permissions(id) ON DELETE CASCADE,
    granted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (client_id, permission_id)
);
-- api_clients.user_id: dono da chave (NULL = chave de serviço), ON DELETE CASCADE

-- Tokens de refresh (controle de sessão)
CREATE TABLE refresh_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked     BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    ip_address  INET,
    user_agent  TEXT
);
```

### Dados iniciais (seed via Alembic)

Aplicação `trem` (esta API), com as permissões:

| Chave | Acesso |
|-------|--------|
| `trem:file_editor` | Manipulação de arquivos |
| `trem:airline_company` | Informações de empresas aéreas |
| `trem:ai` | Modelo de linguagem (`/llm`) |
| `trem:linkedin_scraper` | Jobs de scraping do LinkedIn |
| `trem:admin` | Administração (005); concedida a ninguém pela migration |

A migration 004 trocou o perfil único (`profile_id`) pelo conjunto: todo usuário
existente recebeu todos os perfis, e toda chave existente manteve o seu e ganhou
`ai` (antes dela o `/llm` aceitava qualquer chave). A 005 transformou os perfis,
antes um enum travado por CHECK, em permissões da aplicação `trem`, sem mudar nenhum
acesso: aplicação ou permissão nova é um registro, não uma migration.
`users.profile_id` e `api_clients.profile_id` continuam no banco, opcionais e sem uso
pelo código, para o downgrade da 004 devolver o perfil exato de cada linha.

### Status possíveis de usuário

| Status      | Significado                                      |
|-------------|--------------------------------------------------|
| `pending_verification` | Cadastro próprio, e-mail ainda não confirmado |
| `pending_approval` | E-mail confirmado, aguardando o administrador |
| `blocked`   | Padrão na criação pelo admin. Admin deve habilitar |
| `active`    | Acesso liberado                                  |
| `suspended` | Suspensão manual pelo administrador              |
| `rejected`  | Cadastro recusado pelo administrador             |

---

## Autenticação — dois esquemas, uma autorização

A API é consumida por **aplicações** (outras APIs, scripts, o playground) e,
futuramente, por **usuários humanos** num front-end. Os dois obtêm credenciais
de formas diferentes, mas passam pela mesma checagem de permissão.

| Chamador | Credencial | Header |
|----------|-----------|--------|
| Aplicação | API key por cliente | `X-API-Key: trem_...` |
| Usuário | JWT RS256 | `Authorization: Bearer ...` |
| Administrador | JWT com `trem:admin` e `amr` contendo `otp` | `Authorization: Bearer ...` |

As duas primeiras são normalizadas em um `Principal` (`app/domain/entities/api_client.py`),
que é o que `require_permission()` inspeciona. As rotas não sabem — nem precisam
saber — qual esquema foi usado.

A administração (`/admin/*`, `/users`) exige uma **pessoa**: login JWT de quem tem
`trem:admin`, feito com segundo fator (TOTP). Uma senha vazada ou uma API key não
chegam lá. A antiga master key (`API_KEY`) não administra mais nada.

### Segundo fator (TOTP)

- RFC 6238 (SHA-1, 6 dígitos, 30 s, um passo de tolerância), implementado em
  `app/core/totp.py` e conferido contra os vetores da RFC.
- Segredo de 160 bits cifrado com AES-256-GCM (`MFA_ENCRYPTION_KEY`, fora do banco), com
  o id do usuário como dado associado: copiado para outra conta, não decifra.
- O último passo aceito fica gravado (`totp_last_step`): o mesmo código não entra duas
  vezes, nem em corrida (`UPDATE ... WHERE totp_last_step < :step`).
- 10 códigos de recuperação de uso único, SHA-256 no banco, mostrados só na criação.
- Tentativas limitadas por conta (`MFA_MAX_ATTEMPTS` por `MFA_WINDOW_SECONDS`).
- Cadastrar, regenerar códigos ou desativar pede a senha de novo; desativar encerra
  todas as sessões.

Login com TOTP ativo é em dois passos:

```
POST /auth/login       → {"mfa_required": true, "mfa_token": "..."}   (5 min, não abre rota nenhuma)
POST /auth/mfa/verify  {"mfa_token", "code" | "recovery_code", "session"} → tokens, amr ["pwd","otp"]
```

O `amr` fica no refresh token e passa para os tokens renovados.

### Primeiro admin

```bash
docker exec -it trem-api python -m scripts.bootstrap_admin diego --email voce@example.com
```

Cria a conta ativa com `trem:admin` e senha provisória (mostrada uma vez), ou promove uma
conta existente. Depois: trocar a senha, cadastrar o TOTP e entrar de novo com o código.

### API keys por cliente

Cada aplicação consumidora tem a sua chave, com permissões e revogação próprias
(tabela `api_clients`). Isso substitui a chave única compartilhada, que não
permitia revogar um consumidor sem derrubar todos nem saber quem chamou o quê.

- Chave gerada com 256 bits de entropia, prefixada com `trem_`
- Armazenada como **SHA-256**, não Argon2id — a chave é aleatória, então não há
  dicionário a encarecer, e o hash determinístico é o que permite localizar o
  cliente por índice em vez de varrer a tabela a cada request
- Devolvida em claro **uma única vez**, na criação
- `last_used_at` registra o uso, para auditoria e detecção de chaves esquecidas
- Pode ter um **dono** (`user_id`). As permissões da chave precisam estar entre as do
  dono na criação, e a cada request valem só as que o dono ainda tem: bloquear o
  usuário ou tirar uma permissão dele vale na hora para as chaves dele. O `Principal`
  carrega o `user_id`, que é a quem pertencem os recursos pessoais (jobs de
  scraping), para sobreviverem à rotação da chave. Sem dono é chave de serviço

```bash
# Bootstrap (primeiro cliente, antes de existir chave para chamar a API)
python -m scripts.manage_api_clients create files-app --permission trem:file_editor
python -m scripts.manage_api_clients create diego-ai --permission trem:ai --permission trem:linkedin_scraper --user diego
python -m scripts.manage_api_clients list
python -m scripts.manage_api_clients revoke <client_id>
```

### Chaves do próprio usuário (`/me/api-keys`)

Quem está logado (JWT; uma chave não cria chaves) gerencia as próprias:

| Método | Path | Descrição |
|--------|------|-----------|
| GET    | `/me` | Perfil, permissões, TOTP, quantas chaves ativas |
| GET    | `/me/api-keys` | Lista, sem a chave: só `key_hint` (`trem_xxxx`) |
| POST   | `/me/api-keys` | `{name, permissions?}` → a chave completa, só nesta resposta |
| DELETE | `/me/api-keys/{id}` | Revoga |

- No máximo **5 chaves ativas** por usuário; revogar libera a vaga e o nome.
- Permissões: subconjunto das `trem:*` do dono, nunca `trem:admin` (admin exige pessoa
  com segundo fator). Sem `permissions`, a chave leva todas as `trem:*` do dono.
- O nome é único entre as chaves ativas do mesmo dono (migration 009); dois usuários
  podem ter uma chave "notebook".
- Chave de outro usuário responde 404, igual a uma que não existe.

### Precedência

Quando as duas credenciais chegam juntas, a **API key vence** e o Bearer é
ignorado — inclusive se estiver inválido. Sem isso, um header `Authorization`
velho deixado pelo browser derrubaria com 401 uma chamada legítima por chave.

---

## Autenticação de usuários — JWT (RS256 + Argon2id)

### Tecnologias

| Componente      | Tecnologia         | Motivo                                              |
|-----------------|--------------------|-----------------------------------------------------|
| Hash de senha   | **Argon2id**       | Vencedor do PHC, recomendado pelo OWASP             |
| Algoritmo JWT   | **RS256**          | Assimétrico: outros serviços verificam sem a chave privada |
| Biblioteca JWT  | **PyJWT**          | Mantida ativamente, RS256 completo; substituiu python-jose (semi-abandonada, CVE-2024-33663/33664) |
| Refresh token   | 256 bits opacos, SHA-256 no banco | Rotação a cada uso, com detecção de reuso |

### Parâmetros Argon2id (OWASP recomendado)

```
memory_cost  = 64 MB (65536 KiB)
time_cost    = 3 iterações
parallelism  = 4 threads
```

### Estrutura dos tokens

**Access token (15 min)**, emitido para uma aplicação (`aud`), com header `kid`:
```json
{
  "iss": "https://api.didilv93.com",
  "aud": "trem",
  "sub": "uuid-do-usuario",
  "jti": "id-unico",
  "username": "nome_usuario",
  "perms": ["trem:ai", "trem:file_editor"],
  "amr": ["pwd"],
  "must_change_password": false,
  "iat": 1234567890,
  "exp": 1234567890,
  "type": "access"
}
```

`perms` traz só as permissões daquela aplicação: um token emitido para `financas`
não abre a trem.API (o `aud` é conferido) nem revela o que o usuário tem em outras.

**Refresh token (7 dias):** opaco, 256 bits aleatórios. O banco guarda só o SHA-256.

- **Rotação**: cada `/auth/refresh` devolve um par novo e revoga o token usado, que
  passa a apontar para o sucessor (`replaced_by`). Os tokens de um mesmo login
  dividem a `family_id`.
- **Detecção de reuso**: um token já trocado que volta a ser usado significa que
  alguém tem uma cópia. A família inteira é revogada, inclusive o token mais novo
  do dono legítimo, que precisa logar de novo. Dois refreshes simultâneos com o
  mesmo token contam como reuso (o "claim" é um `UPDATE ... WHERE revoked = false`).
- **Ligado à aplicação**: o refresh só emite tokens para a aplicação do login. Perder
  todas as permissões nela, ou ser bloqueado, encerra o login no próximo refresh.
- **Logout** encerra o login inteiro (a família), não só o token apresentado.

### Verificação por outras aplicações (JWKS)

`GET /.well-known/jwks.json` publica a chave pública de assinatura. Outra aplicação
valida o token localmente, sem chamar a trem.API: assinatura RS256 pela chave do
`kid`, `iss` igual ao da trem.API, `aud` igual ao próprio slug, `exp`. O `kid` é o
thumbprint RFC 7638 da chave.

Troca de chave sem derrubar ninguém: a chave antiga vai para `JWT_EXTRA_PUBLIC_KEYS`
(separadas por `|||`) enquanto seus tokens vivem (15 min), e sai depois.

### Sessão do navegador

Com `"session": "cookie"` no login, o refresh vai num cookie `trem_refresh`
`HttpOnly; Secure; SameSite=None; Partitioned; Path=/auth`, fora do alcance do
JavaScript, e o access token fica só em memória. Os fronts ficam em outros domínios e
chamam a API direto: o cookie precisa viajar entre sites, e `Partitioned` (CHIPS) o
guarda separado por site do front, aceito pelo Chrome e pelo Firefox mesmo onde
bloqueiam cookie de terceiro. `/auth/refresh` e `/auth/logout` sem corpo leem o cookie
e exigem o header `X-Requested-With: trem`: um formulário de outro site não envia header
próprio, e um `fetch` que envie passa por preflight, que o CORS só aprova para
`CORS_ORIGINS`. Ao gravar o cookie, a API expira o antigo `SameSite=Strict` sem
partição: enviado junto, o token já rotacionado contaria como reuso. Em HTTP local
(`COOKIE_SECURE=false`) o cookie segue `SameSite=Strict`, sem partição.

Sem `session`, o refresh volta no corpo (aplicações, scripts, outros fronts).

### Página (`/`)

`app/static/account.js` cuida da sessão e da aba Account; `script.js` faz todas as
chamadas por `authFetch`. A API key digitada no campo tem prioridade sobre o login (quem
cola uma chave logado quer testar aquela chave); sem chave, vai o Bearer da sessão, e um
401 da sessão dispara um refresh e uma nova tentativa. A aba Account usa `sessionFetch`,
sempre com o login: `/me` e `/auth` não aceitam API key. Ao lado do campo, a página diz
qual credencial está em uso.

- A API key fica só no campo: não é gravada em `localStorage` nem `sessionStorage`, e
  some ao recarregar. A cópia que versões anteriores deixavam no `localStorage` é apagada
  ao carregar.

- Access token só em memória; refresh no cookie HttpOnly (`session: "cookie"`). Uma flag
  sem segredo no `localStorage` diz se vale tentar restaurar a sessão ao carregar.
- Refreshes serializados entre abas com Web Locks: duas abas reapresentando o mesmo
  cookie disparariam a detecção de reuso e encerrariam o login. Logout é avisado às
  outras abas por `BroadcastChannel`.
- Links de confirmação (`#verify-email=`) e de redefinição de senha (`#reset-password=`)
  saem da barra de endereço e do histórico ao carregar; o de redefinição fica só em
  memória até a senha nova ser enviada.
- Chave nova e códigos de recuperação aparecem num diálogo que é esvaziado ao fechar.
- O segredo TOTP aparece como texto e link `otpauth:`, sem QR code: nenhum script de
  terceiros numa página que manipula segredos.

### Sessão do banco

`get_db` é sempre `Depends(get_db, scope="function")`: o commit acontece antes de a
resposta sair. No escopo padrão do FastAPI 0.141 ("request") ele rodava depois, e um
cliente que agia logo após um 2xx podia não encontrar o dado gravado.

### Limite de tentativas de login

Em memória (processo único), janela deslizante de `LOGIN_FAILURE_WINDOW_SECONDS` (15 min):
`LOGIN_MAX_FAILURES_PER_USERNAME` (5) por username e `LOGIN_MAX_FAILURES_PER_IP` (20) por IP.
Acima disso, 429 com `Retry-After`. O username conta mesmo quando não existe, para o
bloqueio não revelar quais contas existem; pelo mesmo motivo, login de usuário
inexistente roda uma verificação Argon2id contra um hash descartável e leva o mesmo
tempo que uma senha errada. O IP vem de `CF-Connecting-IP`, que o Cloudflare
sobrescreve; o primeiro item do `X-Forwarded-For` é escolhido pelo cliente.

---

## Cadastro pelo próprio usuário

```
1. POST /auth/register {username, email, password}
   → conta pending_verification, sem permissões; resposta 202 sempre igual
   → e-mail com link {FRONTEND_URL}/#verify-email=<token> (256 bits, uso único, 24 h)

2. POST /auth/verify-email {token}
   → status pending_approval, email_verified_at preenchido
   → e-mail para os admins (usuários ativos com trem:admin) e para ADMIN_EMAIL
     (ou EMAIL_RECIPIENT), que cobre o tempo antes de existir o primeiro admin

3. Admin aprova e define as permissões (API de admin)
```

- **Username é público, e-mail não**: o formulário confere o nome enquanto ele é
  digitado (`GET /auth/username-available?username=`, sem login, `{available}`), e o
  cadastro com nome já usado dá 409. Um nome aparece para outras pessoas e o login já
  limita tentativas por nome; esconder custava um cadastro inteiro de ida e volta pela
  caixa de entrada. O e-mail continua sem enumeração: já usado recebe a mesma resposta
  202, e quem fica sabendo é só o dono do endereço, por e-mail. A senha é hasheada antes
  de qualquer consulta, e os e-mails saem numa background task depois da resposta, então
  o tempo também não muda.
- **Token no fragmento** (`#`): não vai para log do nginx nem para o `Referer`.
- **Cadastro abandonado** (não confirmado em `EMAIL_TOKEN_TTL_HOURS`) não segura o
  username nem o e-mail: um novo cadastro apaga a conta pendente.
- **Reenvio**: `POST /auth/resend-verification {email}` invalida os links anteriores.
- **Limites**: `SIGNUP_MAX_PER_IP` (5) cadastros por IP por `SIGNUP_WINDOW_SECONDS` (1 h);
  o reenvio conta por IP e por e-mail; a conferência do username,
  `USERNAME_CHECK_MAX_PER_IP` (60) por `USERNAME_CHECK_WINDOW_SECONDS` (5 min).
  `SIGNUP_ENABLED=false` fecha o cadastro.
- **Login** de conta pendente diz o motivo (e-mail não confirmado, aguardando
  aprovação) só depois da senha certa.
- **Senha**: mínimo 12, máximo 128, sem regra de composição (NIST SP 800-63B); não pode
  conter o username nem o e-mail, nem ser quase toda o mesmo caractere. Vale também
  para a troca de senha.
- **Senha vazada**: cadastro, troca e redefinição consultam o Have I Been Pwned pela API
  de range (k-anonimidade): sai do servidor só o prefixo de 5 caracteres do SHA-1, com
  `Add-Padding`, e a comparação do sufixo é local. Achou, 422. Se o serviço não responde
  em `PWNED_PASSWORDS_TIMEOUT` (3 s), a senha é aceita: uma queda de terceiro não trava
  cadastro nem redefinição. `PWNED_PASSWORDS_ENABLED=false` desliga. Na troca, a consulta
  só acontece depois de a senha atual ser conferida.

## Esqueci a senha

```
1. POST /auth/forgot-password {email}
   → 202 sempre igual; só conta active recebe o link
   → e-mail com link {FRONTEND_URL}/#reset-password=<token> (256 bits, uso único, 60 min)

2. POST /auth/reset-password {token, new_password}
   → senha trocada, must_change_password=False
   → todos os refresh tokens revogados, bloqueio de login zerado
   → e-mail avisando o dono da conta
```

- Mesma tabela `email_tokens` do cadastro, `purpose='reset_password'`. Um pedido novo
  invalida os links anteriores.
- Conta pendente, suspensa, recusada ou bloqueada não recebe link: a pendente tem o
  próprio link de confirmação, e as outras dependem do admin.
- A política de senha (inclusive username e e-mail da conta) é checada **antes** de
  gastar o token: senha recusada não queima o link.
- **TOTP continua valendo**: o e-mail prova a caixa de entrada, não o segundo fator.
- Limite: o mesmo `SIGNUP_MAX_PER_IP` por `SIGNUP_WINDOW_SECONDS`, contado por IP e por
  e-mail. `PASSWORD_RESET_TTL_MINUTES` (60) controla a validade.
- API keys do usuário continuam ativas: elas têm revogação própria em `/me/api-keys`.

## Fluxo Completo de Cadastro e Primeiro Acesso

```
1. Admin → POST /users
   → cria usuário com status='blocked', must_change_password=True
   → sem senha definida ainda

2. Admin → POST /users/{id}/send-provisional-password
   → gera senha segura aleatória (14 chars: upper+lower+digit+special)
   → armazena hash Argon2id no banco
   → registra provisional_password_sent_at = agora
   → envia email com a senha em texto puro

3. Admin → [UPDATE users SET status='active' WHERE id=?]
   → habilitação manual no banco (por enquanto)

4. Usuário → POST /auth/login
   → valida username + senha
   → verifica status = 'active' (se bloqueado → 403)
   → retorna access_token (15min) + refresh_token (7 dias)
   → payload do access_token inclui must_change_password=true

5. Qualquer endpoint protegido (exceto /auth/change-password)
   → middleware detecta must_change_password=true → 403

6. Usuário → POST /auth/change-password
   → valida senha atual
   → armazena novo hash Argon2id
   → must_change_password = false
   → revoga todos os refresh_tokens anteriores
   → emite novos tokens

7. Usuário acessa endpoints conforme as permissões
```

---

## Endpoints da API

### Auth (`/auth`)

| Método | Path                    | Auth       | Descrição                        |
|--------|-------------------------|------------|----------------------------------|
| POST   | `/auth/login`           | Público    | Login; `audience` (padrão `trem`) e `session` (`body` ou `cookie`) |
| POST   | `/auth/refresh`         | Refresh    | Novo par de tokens (corpo ou cookie + `X-Requested-With`) |
| POST   | `/auth/change-password` | Bearer     | Altera senha (obrigatório no 1º acesso) |
| POST   | `/auth/logout`          | Refresh    | Encerra o login (corpo ou cookie + `X-Requested-With`) |
| GET    | `/.well-known/jwks.json`| Público    | Chaves públicas para validar os tokens |

### Users (`/users`) — Admin

| Método | Path                                     | Auth     | Descrição                       |
|--------|------------------------------------------|----------|---------------------------------|
| POST   | `/users`                                 | Admin    | Cria novo usuário               |
| GET    | `/users`                                 | Admin    | Lista usuários (paginado)       |
| GET    | `/users/{id}`                            | Admin    | Detalha usuário                 |
| POST   | `/users/{id}/send-provisional-password`  | Admin    | Envia senha provisória por email|

### Administração (`/admin`) — admin com TOTP

| Método | Path | Descrição |
|--------|------|-----------|
| GET    | `/admin/users?status=pending_approval` | Lista, com filtro de status |
| GET    | `/admin/users/{id}` | Detalha |
| POST   | `/admin/users/{id}/approve` | `{"permissions": [...]}`: ativa com exatamente essas |
| POST   | `/admin/users/{id}/reject` | Recusa o cadastro |
| POST   | `/admin/users/{id}/suspend` | Bloqueia, encerra sessões, para as API keys do dono |
| POST   | `/admin/users/{id}/reactivate` | Volta de suspensa para ativa |
| PUT    | `/admin/users/{id}/permissions` | Substitui o conjunto e encerra as sessões |
| GET/POST | `/admin/applications` | Lista (com permissões) / cria `{slug, name}` |
| DELETE | `/admin/applications/{slug}` | Remove, com permissões, concessões e sessões |
| POST   | `/admin/applications/{slug}/permissions` | Cria `{name}` |
| DELETE | `/admin/applications/{slug}/permissions/{name}` | Remove |

Proteções: o admin não muda o estado da própria conta nem tira o próprio `trem:admin`;
a aplicação `trem` e suas permissões (usadas pelo código) não podem ser removidas.
Trocar permissões encerra as sessões porque os access tokens carregam as permissões
que as outras aplicações leem; os já emitidos valem até expirar (15 min).

### Catálogo do trem.FRONT (`/hub`, `/admin/hub`)

Tabela `hub_services`: nome, descrição, endereço, ativo, ordem e logo (bytes no banco,
tipo lido dos bytes: PNG, WebP ou SVG, até 256 KB). Cada serviço pertence a uma permissão
`hub:<slug>` (FK com `ON DELETE CASCADE`): criar o serviço cria a permissão, removê-lo
remove a permissão e as concessões.

| Método | Path | Descrição |
|--------|------|-----------|
| GET    | `/hub/services` | Serviços da conta logada: ativos, com endereço e concedidos |
| GET    | `/hub/services/{slug}/logo?v=` | Público, para `<img>`; `v` (hash) torna a URL imutável |
| GET/POST | `/admin/hub/services` | Lista todos / cria com a permissão |
| PATCH  | `/admin/hub/services/{slug}` | Só os campos enviados |
| PUT    | `/admin/hub/services/order` | Nova ordem, com todos os serviços |
| DELETE | `/admin/hub/services/{slug}` | Remove com permissão e concessões |
| PUT/DELETE | `/admin/hub/services/{slug}/logo` | Envia (multipart `file`) / remove |
| PUT/DELETE | `/admin/hub/services/{slug}/users/{id}` | Libera / tira o acesso da conta |

Liberar ou tirar um serviço **não** encerra sessões, ao contrário de
`PUT /admin/users/{id}/permissions`: o catálogo é lido do banco a cada visita e `hub:*`
não vai em token da `trem`. O SVG é servido com CSP `sandbox` e `nosniff`: aberto direto
não executa nada, e dentro de `<img>` o navegador já não executaria. A migration 010 traz
os cinco serviços iniciais com logo e sem endereço (fora do catálogo até o admin preencher).

### MFA (`/auth/mfa`) — usuário logado

| Método | Path | Descrição |
|--------|------|-----------|
| POST | `/auth/mfa/enroll` | `{password}` → `{secret, otpauth_uri}` (ainda inativo) |
| POST | `/auth/mfa/confirm` | `{code}` → ativa e devolve os códigos de recuperação |
| POST | `/auth/mfa/recovery-codes` | `{password, code}` → novo conjunto |
| POST | `/auth/mfa/disable` | `{password, code}` → desativa e encerra as sessões |
| POST | `/auth/mfa/verify` | Segundo passo do login |

`POST /users` recebe `permissions` (lista de chaves `<aplicação>:<permissão>`, ao menos
uma). `POST /admin/clients` recebe `permissions` e, opcionalmente, `user_id` do dono.
Os antigos `profile_name` e `profile_names` respondem 422.

### Arquivos — permissão `trem:file_editor`

| Método | Path                  |
|--------|-----------------------|
| POST   | `/pdf/split`          |
| POST   | `/pdf/merge`          |
| POST   | `/pdf/*`              |
| POST   | `/movie/cut`          |
| POST   | `/movie/transcribe`, `/audio/transcribe` (job) |
| POST/PUT/DELETE | `/movie/transcribe/uploads/*`, `/audio/transcribe/uploads/*` (upload em partes) |
| GET    | `/movie/transcribe/jobs/{id}`, `/audio/transcribe/jobs/{id}` |
| POST   | `/audio/*`            |
| POST   | `/image/*`            |

### Aéreas — permissão `trem:airline_company`

| Método | Path            | Descrição                              |
|--------|-----------------|----------------------------------------|
| GET    | `/airline/*`    | Proxy para API aérea na rede Docker    |

### LLM — permissão `trem:ai`

| Método | Path                         | Descrição                                   |
|--------|------------------------------|---------------------------------------------|
| POST   | `/llm/v1/chat/completions`   | Chat no formato OpenAI, sem streaming       |
| POST   | `/llm/jobs`                  | Mesmo chat como job; responde `job_id`      |
| GET    | `/llm/jobs/{job_id}`         | Estado e resultado do job                   |
| GET    | `/llm/health`                | Repassa o `/health` do `trem-llm`           |

O modelo (gpt-oss-120b no llama-server) roda no container `trem-llm`, na rede Docker
`trem-llm` (`--internal`, sem porta no host). A trem-api é ligada a ela no deploy e
fala com `http://trem-llm:8080` usando `LLM_API_KEY`; nada do chamador é repassado.

- O endpoint síncrono só serve pela porta local (`127.0.0.1:13002`): o modelo descarrega
  após 15 min ocioso, a recarga leva ~3,5 min, e o Cloudflare corta em 100 s e o nginx em
  60 s. Pela URL pública o cliente usa `/llm/jobs` e consulta o resultado, como na
  transcrição. `LLM_TIMEOUT` é 600 s.
- Uma inferência por vez (`--parallel 1`): fila em memória com até `LLM_QUEUE_SIZE`
  requisições; acima disso, 503 com `Retry-After`. Exige processo único.
- O servidor força `stream: false`, remove `model` e limita `max_tokens` a `LLM_MAX_TOKENS`.
  Corpo limitado a `LLM_MAX_REQUEST_SIZE` (64 KB).
- Sem retry: repetir uma inferência de minutos só dobra a espera.
- Log de uma linha por requisição, só com métricas. Corpo, mensagens e resposta nunca
  vão para log nem para `detail` de erro (CVs e vagas são dados pessoais).
- Se o cliente desconectar, a inferência continua até o fim ocupando o modelo.
- Modelo dormindo (`GET /props` → `is_sleeping`): a chamada recebe na hora `503`
  `warming_up` com `Retry-After`, e a API dispara uma única chamada para acordá-lo.
  O llama-server não informa o progresso da carga, então `eta_seconds` e `progress`
  são estimados a partir de `LLM_WARMUP_SECONDS` (recarga medida, 210 s).
  `/llm/health` responde `healthy`, `sleeping` ou `warming_up` sem acordar o modelo.
- Jobs (`LlmJobs`, em memória): dividem a fila e o aquecimento com o síncrono. A vaga na
  fila é reservada no `POST`, então fila cheia é 503 na hora, não um job que falha depois.
  Com o modelo dormindo, o job espera o aquecimento em vez de responder 503. Job
  terminado guarda o resultado (dado pessoal) por `LLM_JOB_TTL_SECONDS` (1 h); jobs se
  perdem se o processo reiniciar.
- A aba **AI** da página `/` é um chat sobre os jobs: mostra o estado do modelo e o
  aquecimento (barra estimada por `LLM_WARMUP_SECONDS`), começa a acordá-lo quando o
  usuário digita, e retoma o polling de respostas pendentes após recarregar a página.
  O histórico fica só no `localStorage` do navegador.

### Scrapers — permissão `trem:linkedin_scraper`

| Método | Path | Descrição |
|--------|------|-----------|
| *      | `/scrapers/linkedin/*` | Proxy para o orquestrador do linkedinho na rede Docker |

- A trem-api só autentica e repassa (`app/routers/scraperRoute.py`, cliente em
  `infrastructure/http/scraper_client.py`). Jobs, CVs, resultados e sessões do LinkedIn ficam
  no orquestrador, cifrados por dono.
- O dono é o `Principal.user_id`: o usuário logado ou o dono da chave. Chave de serviço sem
  dono recebe 403. A trem-api envia `X-Owner-Id` e `X-Internal-Key` (`SCRAPER_API_KEY`) e
  descarta os que vierem do chamador; o orquestrador só confia no dono junto com essa chave.
- O id do job precisa ser 32 hex antes de ir para a URL do orquestrador.
- Erros do próprio pedido (400/404/409/413/422) passam com o `detail`; falha do orquestrador
  vira 502 sem detalhe, indisponível 503, timeout 504. Sem retry: criar job ou enfileirar
  execução duas vezes não é inofensivo.
- A aba **Scrapers** da página `/` lista os jobs com status, próxima e última execução, cria e
  edita, envia o CV, pausa/retoma/executa e mostra os matches. Atualiza sozinha a cada 30 s.
- **Login remoto do LinkedIn.** `POST /session/login` faz o orquestrador abrir um Chromium só
  daquele usuário, num display próprio, transmitido por x11vnc + websockify presos ao
  localhost do container. A trem-api devolve uma URL com token de uso único (2 min,
  `app/services/scraperLoginService.py`); abrir a URL troca o token por um cookie aleatório
  `HttpOnly`, `SameSite=Strict`, com path restrito àquele login, e redireciona ao noVNC. Os
  arquivos do noVNC e o WebSocket (`/scrapers/linkedin/login/{id}/...`) só aceitam esse
  cookie; o WebSocket exige também `Origin` igual ao host. Só digests de token e cookie ficam
  em memória (processo único).
- A senha vai do teclado da pessoa para a página do LinkedIn naquele navegador: os quadros do
  VNC passam pela trem-api, mas nada os lê nem os grava. A confiança é no operador do servidor,
  como em qualquer navegador remoto.
- O login segura a trava do dono (nunca divide o perfil com uma coleta), dura no máximo 10 min
  e no máximo 2 rodam ao mesmo tempo. Em produção o nginx precisa repassar `Upgrade`/`Connection`
  em `/scrapers/linkedin/login/`.

### Transcrição — upload em partes e job em segundo plano

O Cloudflare recusa corpos acima de 100 MB e respostas que levam mais de 100 s.
Por isso a transcrição não responde na mesma requisição: o arquivo chega em
partes de 50 MB e o resultado é consultado depois (`app/services/transcriptionService.py`).

- Estado em memória (uploads e jobs), com dono = principal que criou. A API precisa
  rodar com um único processo.
- Um job por vez numa thread dedicada: o Whisper satura a CPU e o modelo é compartilhado.
- Arquivos em `UPLOAD_DIR/transcriptions/{id}/`, incluindo o WAV intermediário.
  O diretório é apagado quando o job termina (antes de o status virar `done`/`failed`),
  quando o upload é cancelado ou expira (24 h sem atividade).
- No startup o diretório inteiro é apagado: depois de um crash nada ali pertence a um
  upload vivo. Uma varredura a cada 10 min remove expirados e diretórios órfãos antigos.

---

## Controle de Permissões

```python
@router.post("/pdf/split")
async def split_pdf(
    ...,
    _: Principal = Depends(require_permission(TremPermission.FILE_EDITOR))
):
    ...
```

A permissão vem do banco a cada request, nunca do token: tirar uma permissão vale
na hora, sem esperar o access token expirar.

---

## Comunicação com Serviços Externos (Docker)

O cliente HTTP (`infrastructure/http/airline_client.py`) comunica-se por nome de serviço na rede Docker, sem IP fixo:

```
http://airline-api:PORT/endpoint
```

Recursos do cliente:
- Timeouts configuráveis por ambiente
- Retry com backoff exponencial (3 tentativas)
- Health-check do serviço antes de repassar requisições
- Propagação do Bearer token do usuário (ou chave interna de serviço)

---

## Docker Compose

```yaml
networks:
  trem-network:
    driver: bridge

services:
  trem-api:
    build: .
    networks: [trem-network]

  postgres:
    image: postgres:16-alpine
    networks: [trem-network]

  adminer:              # painel web gratuito para PostgreSQL
    image: adminer
    networks: [trem-network]
    ports: ["8080:8080"]

  airline-api:          # futuro serviço de empresas aéreas
    networks: [trem-network]
```

---

## Stack — Apenas Recursos Gratuitos/Open-Source

| Necessidade         | Ferramenta            |
|---------------------|-----------------------|
| Web Framework       | FastAPI               |
| ORM + Async         | SQLAlchemy 2.x async  |
| Migrações           | Alembic               |
| Hash de senha       | argon2-cffi (Argon2id)|
| JWT                 | PyJWT[crypto]         |
| Config segura       | pydantic-settings     |
| Cliente HTTP        | httpx                 |
| DB admin web        | Adminer (Docker)      |
| Rate limiting       | slowapi               |
| Driver async PG     | asyncpg               |

---

## Geração das Chaves RS256

Execute uma vez para gerar o par de chaves:

```bash
python scripts/generate_keys.py
```

O script gera `private_key.pem` e `public_key.pem`. Copie o conteúdo para o `.env`:

```
JWT_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"
```

---

## Fases de Implementação

| Fase | Descrição                                                         | Status |
|------|-------------------------------------------------------------------|--------|
| 1    | Estrutura de diretórios + ARCHITECTURE.md                        | ✅     |
| 2    | Interfaces abstratas (`core/interfaces/`)                         | ✅     |
| 3    | PostgreSQL: modelos ORM + Alembic + primeira migration            | ✅     |
| 4    | Autenticação: Argon2id + JWT RS256 + fluxo completo              | ✅     |
| 5    | CRUD de usuários + fluxo de senha provisória                     | ✅     |
| 6    | Migrar routers existentes para JWT + permissões                  | ✅     |
| 7    | Cliente HTTP para API aérea                                       | ✅     |
| 8    | Docker Compose com postgres, adminer e rede compartilhada        | ✅     |
| 9    | Testes e CI/CD                                                    | 🔲     |
