# trem.API — Arquitetura e Plano de Implementação

## Visão Geral

trem.API é uma plataforma de processamento de arquivos com identidade própria, autenticação segura, controle de perfis e capacidade de integração com serviços externos na mesma rede Docker.

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
│   │   └── dependencies.py      # get_current_user, require_profile, get_db
│   └── middleware/
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
│   │   └── profile.py
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
│   │   └── airline_client.py    # Cliente HTTP p/ API aérea na rede Docker
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
-- Perfis de acesso
CREATE TABLE profiles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(50) UNIQUE NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Usuários
CREATE TABLE users (
    id                           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username                     VARCHAR(100) UNIQUE NOT NULL,
    email                        VARCHAR(255) UNIQUE,
    password_hash                TEXT NOT NULL,
    profile_id                   UUID NOT NULL REFERENCES profiles(id),
    status                       VARCHAR(20) NOT NULL DEFAULT 'blocked',
    must_change_password         BOOLEAN NOT NULL DEFAULT TRUE,
    provisional_password_sent_at TIMESTAMPTZ,
    created_at                   TIMESTAMPTZ DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ DEFAULT NOW(),
    last_login_at                TIMESTAMPTZ
);

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

```sql
INSERT INTO profiles (name, description) VALUES
  ('file_editor',     'Acesso aos serviços de manipulação de arquivos'),
  ('airline_company', 'Acesso a informações de empresas aéreas');
```

### Status possíveis de usuário

| Status      | Significado                                      |
|-------------|--------------------------------------------------|
| `blocked`   | Padrão no cadastro. Admin deve habilitar         |
| `active`    | Acesso liberado                                  |
| `suspended` | Suspensão manual pelo administrador              |

---

## Autenticação — Bearer Token JWT (RS256 + Argon2id)

### Tecnologias

| Componente      | Tecnologia         | Motivo                                              |
|-----------------|--------------------|-----------------------------------------------------|
| Hash de senha   | **Argon2id**       | Vencedor do PHC, recomendado pelo OWASP             |
| Algoritmo JWT   | **RS256**          | Assimétrico: outros serviços verificam sem a chave privada |
| Biblioteca JWT  | **PyJWT**          | Mantida ativamente, RS256 completo; substituiu python-jose (semi-abandonada, CVE-2024-33663/33664) |
| Hash de refresh token | SHA-256 | Token raw não fica no banco                    |

### Parâmetros Argon2id (OWASP recomendado)

```
memory_cost  = 64 MB (65536 KiB)
time_cost    = 3 iterações
parallelism  = 4 threads
```

### Estrutura dos tokens

**Access token (15 min):**
```json
{
  "sub": "uuid-do-usuario",
  "username": "nome_usuario",
  "profile": "file_editor",
  "must_change_password": false,
  "iat": 1234567890,
  "exp": 1234567890,
  "type": "access"
}
```

**Refresh token (7 dias):**
```json
{
  "sub": "uuid-do-usuario",
  "jti": "uuid-unico-do-token",
  "iat": 1234567890,
  "exp": 1234567890,
  "type": "refresh"
}
```

---

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

7. Usuário acessa endpoints conforme perfil
```

---

## Endpoints da API

### Auth (`/auth`)

| Método | Path                    | Auth       | Descrição                        |
|--------|-------------------------|------------|----------------------------------|
| POST   | `/auth/login`           | Público    | Login, retorna par de tokens     |
| POST   | `/auth/refresh`         | Bearer     | Renova access token              |
| POST   | `/auth/change-password` | Bearer     | Altera senha (obrigatório no 1º acesso) |
| POST   | `/auth/logout`          | Bearer     | Revoga refresh token             |

### Users (`/users`) — Admin

| Método | Path                                     | Auth     | Descrição                       |
|--------|------------------------------------------|----------|---------------------------------|
| POST   | `/users`                                 | API Key  | Cria novo usuário               |
| GET    | `/users`                                 | API Key  | Lista usuários (paginado)       |
| GET    | `/users/{id}`                            | API Key  | Detalha usuário                 |
| POST   | `/users/{id}/send-provisional-password`  | API Key  | Envia senha provisória por email|

### Arquivos — autenticados via JWT + perfil `file_editor`

| Método | Path                  |
|--------|-----------------------|
| POST   | `/pdf/split`          |
| POST   | `/pdf/merge`          |
| POST   | `/pdf/*`              |
| POST   | `/movie/cut`          |
| POST   | `/movie/transcribe`   |
| POST   | `/audio/*`            |
| POST   | `/image/*`            |

### Aéreas — autenticados via JWT + perfil `airline_company`

| Método | Path            | Descrição                              |
|--------|-----------------|----------------------------------------|
| GET    | `/airline/*`    | Proxy para API aérea na rede Docker    |

---

## Controle de Perfis

```python
# Uso nos routers via Depends
@router.post("/pdf/split")
async def split_pdf(
    ...,
    user: User = Depends(require_profile("file_editor"))
):
    ...

@router.get("/airline/flights")
async def get_flights(
    ...,
    user: User = Depends(require_profile("airline_company"))
):
    ...
```

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
| 6    | Migrar routers existentes para JWT + perfis                      | ✅     |
| 7    | Cliente HTTP para API aérea                                       | ✅     |
| 8    | Docker Compose com postgres, adminer e rede compartilhada        | ✅     |
| 9    | Testes e CI/CD                                                    | 🔲     |
