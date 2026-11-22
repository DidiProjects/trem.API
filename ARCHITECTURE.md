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
│   │   │   ├── me.py            # /me e as chaves do próprio usuário
│   │   │   ├── admin.py         # aplicações e permissões
│   │   │   ├── api_clients.py   # chaves de serviço
│   │   │   ├── hub.py           # catálogo do trem.FRONT
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
│   ├── authentik.py             # validação dos tokens do authentik (JWKS)
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
│       ├── api_client.py        # chaves e /me
│       ├── application.py       # aplicações e permissões
│       └── common.py            # Paginação, envelopes de resposta
│
├── services/
│   ├── authentik_user_service.py # conta ligada ao token do authentik, grupos gravados
│   ├── api_client_service.py    # chaves de API
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

-- Pessoas que entram pelo authentik, ligadas pelo username (preferred_username).
-- Senha, segundo fator e se a conta está ativa ficam no authentik (migration 011).
CREATE TABLE users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username    VARCHAR(100) UNIQUE NOT NULL,
    email       VARCHAR(255) UNIQUE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
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

---

## Autenticação — dois esquemas, uma autorização

| Chamador | Credencial | Header |
|----------|-----------|--------|
| Aplicação | API key por cliente | `X-API-Key: trem_...` |
| Pessoa | Token de acesso do authentik (trem.AUTH) | `Authorization: Bearer ...` |
| Administrador | Token do authentik com `trem:admin` e `amr` contendo `mfa` | `Authorization: Bearer ...` |

As duas são normalizadas em um `Principal` (`app/domain/entities/api_client.py`),
que é o que `require_permission()` inspeciona. As rotas não sabem — nem precisam
saber — qual esquema foi usado.

O login das pessoas é **só o do authentik**. O token é conferido contra
`<AUTHENTIK_ISSUER>jwks/` (RS256, `iss`, `aud` = `AUTHENTIK_AUDIENCE`, `exp`), sem
perguntar nada ao authentik a cada chamada (`app/core/authentik.py`). A conta é achada
pelo `preferred_username`, ou criada na primeira chamada; as permissões são os grupos do
token com nome de permissão (`trem:*`, `hub:<slug>`) e ficam gravadas a cada chamada,
para as chaves pessoais seguirem o authentik. Senha, segundo fator, cadastro, aprovação
e desativação são do authentik.

A administração (`/admin/*`) exige uma **pessoa**: login de quem tem `trem:admin`, feito
com segundo fator. Uma API key não chega lá.

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
  dono na criação, e a cada request valem só as que o dono tem gravadas, que seguem o
  authentik a cada chamada do dono. Uma conta desativada no authentik não chama mais,
  então as chaves dela ficam com as últimas permissões até serem revogadas. O `Principal`
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

Quem está logado (token do authentik; uma chave não cria chaves) gerencia as próprias:

| Método | Path | Descrição |
|--------|------|-----------|
| GET    | `/me` | Perfil, permissões, quantas chaves ativas |
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

## Endpoints da API

### Administração (`/admin`) — admin com segundo fator

| Método | Path | Descrição |
|--------|------|-----------|
| GET/POST | `/admin/applications` | Lista (com permissões) / cria `{slug, name}` |
| DELETE | `/admin/applications/{slug}` | Remove, com permissões, concessões e sessões |
| POST   | `/admin/applications/{slug}/permissions` | Cria `{name}` |
| DELETE | `/admin/applications/{slug}/permissions/{name}` | Remove |

Proteção: a aplicação `trem` e suas permissões (usadas pelo código) não podem ser
removidas.

### Catálogo do trem.FRONT (`/hub`, `/admin/hub`)

Tabela `hub_services`: nome, descrição, endereço, ativo, ordem e logo (bytes no banco,
tipo lido dos bytes: PNG, WebP ou SVG, até 256 KB). Cada serviço pertence a uma permissão
`hub:<slug>` (FK com `ON DELETE CASCADE`): criar o serviço cria a permissão, removê-lo
remove a permissão. Quem vê o serviço é quem está no grupo `hub:<slug>` do authentik.

| Método | Path | Descrição |
|--------|------|-----------|
| GET    | `/hub/services` | Serviços da conta logada: ativos, com endereço e nos grupos dela |
| GET    | `/hub/services/{slug}/logo?v=` | Público, para `<img>`; `v` (hash) torna a URL imutável |
| GET/POST | `/admin/hub/services` | Lista todos / cria com a permissão |
| PATCH  | `/admin/hub/services/{slug}` | Só os campos enviados |
| PUT    | `/admin/hub/services/order` | Nova ordem, com todos os serviços |
| DELETE | `/admin/hub/services/{slug}` | Remove com a permissão (o grupo fica no authentik) |
| PUT/DELETE | `/admin/hub/services/{slug}/logo` | Envia (multipart `file`) / remove |

O SVG é servido com CSP `sandbox` e `nosniff`: aberto direto
não executa nada, e dentro de `<img>` o navegador já não executaria. A migration 010 traz
os cinco serviços iniciais com logo e sem endereço (fora do catálogo até o admin preencher).

`POST /admin/clients` recebe `permissions` e, opcionalmente, `user_id` do dono.
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
  `HttpOnly`, `SameSite=Strict`, com path restrito àquele login, e responde uma página que
  segue sozinha (`meta refresh`) para o noVNC. Não é um 303 porque a aba costuma ser aberta
  por um front em outro site: o navegador mantém essa origem entre sites ao longo do
  redirecionamento e não manda o cookie `Strict` recém-gravado. A partir da página da API, o
  pedido é do mesmo site e o cookie vai. Os
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
| JWT (authentik)     | PyJWT[crypto]         |
| Config segura       | pydantic-settings     |
| Cliente HTTP        | httpx                 |
| DB admin web        | Adminer (Docker)      |
| Rate limiting       | slowapi               |
| Driver async PG     | asyncpg               |

---

## Fases de Implementação

| Fase | Descrição                                                         | Status |
|------|-------------------------------------------------------------------|--------|
| 1    | Estrutura de diretórios + ARCHITECTURE.md                        | ✅     |
| 2    | Interfaces abstratas (`core/interfaces/`)                         | ✅     |
| 3    | PostgreSQL: modelos ORM + Alembic + primeira migration            | ✅     |
| 4    | Autenticação: Argon2id + JWT RS256 + fluxo completo (trocada pelo authentik na 011) | ✅     |
| 5    | CRUD de usuários + fluxo de senha provisória                     | ✅     |
| 6    | Migrar routers existentes para JWT + permissões                  | ✅     |
| 7    | Cliente HTTP para API aérea                                       | ✅     |
| 8    | Docker Compose com postgres, adminer e rede compartilhada        | ✅     |
| 9    | Testes e CI/CD                                                    | 🔲     |
