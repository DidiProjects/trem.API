# OIDC no trem.API — plano

Status: **não implementado**. Este documento registra o que falta para o trem.API ser um
provedor OpenID Connect completo, as decisões de desenho e a ordem de entrega.

## Por que

Hoje cada interface que usa o login do trem.API recebe usuário e senha e chama
`POST /auth/login` com `audience` = slug da aplicação. Funciona, mas:

- a senha (e o código TOTP) passa pela interface, que precisa ser confiável;
- cada interface reimplementa tela de login, TOTP, refresh e troca de senha;
- não há single sign-on: entrar numa interface não vale para a outra.

Com o fluxo *authorization code* + PKCE, a interface redireciona para a página do
trem.API, o usuário entra **lá** (senha e TOTP nunca saem da página do trem.API) e a
interface recebe só um código, que troca por tokens.

## O que já existe e é reaproveitado

| Peça | Onde | Uso no OIDC |
|---|---|---|
| Chave RS256 com `kid` (RFC 7638) e rotação | `app/core/security.py` | assina `id_token` e access token |
| `GET /.well-known/jwks.json` | `routers/auth.py` | `jwks_uri` do discovery |
| `iss` configurável (`JWT_ISSUER`) | `config.py` | `issuer` do discovery |
| Aplicações e permissões `app:perm` | tabelas `applications`, `permissions` | cada aplicação vira um *client* |
| Access token por aplicação (`aud` = slug, `perms`, `amr`) | `create_access_token` | continua igual; `aud` = `client_id` |
| Refresh token rotativo com detecção de reuso | `refresh_tokens` | `grant_type=refresh_token` |
| Sessão do navegador em cookie HttpOnly | `/auth/login` com `session: "cookie"` | sessão SSO da página de login |
| TOTP e `amr` (`pwd`, `otp`) | `mfa_service` | `amr` e `acr` no `id_token` |

## O que falta

### 1. Cadastro de clients (migration)

`applications` ganha as colunas de client OAuth:

- `redirect_uris` (lista, comparação **exata**, sem curinga)
- `post_logout_redirect_uris`
- `client_type`: `public` (SPA, app) ou `confidential` (backend)
- `client_secret_hash` (Argon2id; só para `confidential`, exibido uma vez, como as API keys)
- `allowed_scopes`
- `require_consent` (padrão `false`: as interfaces são próprias)

Rotas de admin: `PUT /admin/applications/{slug}/client` e
`POST /admin/applications/{slug}/client/secret` (rotaciona o secret).

### 2. Endpoints

| Endpoint | Função |
|---|---|
| `GET /.well-known/openid-configuration` | discovery (RFC 8414 / OIDC Discovery) |
| `GET /oauth/authorize` | valida `client_id`, `redirect_uri`, `scope`, `state`, `nonce`, `code_challenge`; sem sessão, manda para a página de login; com sessão, emite o código |
| `POST /oauth/token` | `authorization_code` (+ `code_verifier`) e `refresh_token` |
| `GET /oauth/userinfo` | claims do usuário para o access token |
| `POST /oauth/revoke` | RFC 7009, revoga refresh token |
| `GET /oauth/logout` | RP-initiated logout: encerra a sessão SSO e volta para `post_logout_redirect_uri` |

### 3. Códigos de autorização (tabela nova `authorization_codes`)

- 256 bits, guardado como SHA-256, uso único, vida de 60 s
- amarrado a `client_id`, `redirect_uri`, `user_id`, `scope`, `nonce`, `code_challenge`, `amr`, `auth_time`
- reuso do código revoga os tokens já emitidos com ele (RFC 6749 §4.1.2)

### 4. `id_token`

Claims: `iss`, `sub`, `aud` (= `client_id`), `exp`, `iat`, `auth_time`, `nonce`, `amr`,
`acr`, `azp`. Com scope `profile`: `preferred_username`. Com `email`: `email` e
`email_verified`. Assinado com a mesma chave; `kid` no header.

### 5. Página de login como provedor

A página atual (`account.js`) já faz login, TOTP e troca obrigatória de senha. Falta:

- ler os parâmetros de `/oauth/authorize` e, ao fim do login, voltar para ele
- sessão SSO: o cookie de sessão do trem.API (escopo `/auth`) precisa valer também para
  `/oauth/authorize` — cookie novo de sessão SSO, `SameSite=Lax`, escopo `/oauth`
- `prompt=login` força nova senha; `max_age` compara com `auth_time`
- `acr_values` pedindo TOTP força o segundo fator para quem tem TOTP

## Decisões

- **Só authorization code + PKCE (S256)**, inclusive para clients confidenciais. Sem
  implicit, sem password grant (OAuth 2.1). O `POST /auth/login` atual continua existindo
  para scripts e para a própria página, mas interfaces novas usam OIDC.
- **`redirect_uri` exato**: a principal fonte de vulnerabilidade em provedores caseiros
  é comparar por prefixo.
- **Sem consentimento** por padrão: todas as interfaces são do mesmo dono. A coluna
  `require_consent` fica para um client de terceiro.
- **Escopos**: `openid`, `profile`, `email`, `offline_access` (refresh token só com ele).
  As permissões continuam em `perms` no access token, não viram escopo.
- **Biblioteca**: avaliar `authlib` (servidor OAuth2/OIDC com integração Starlette) contra
  implementação própria. A própria é pequena (os blocos acima), mas o `authlib` já trata
  validação de parâmetros e respostas de erro do RFC. Decidir antes da fase 2.

## Riscos

- **Open redirect** em `redirect_uri` e `post_logout_redirect_uri`: comparação exata e teste
  dedicado.
- **CSRF no login**: `state` obrigatório; a página de login não aceita POST de outra origem
  (o header `X-Requested-With` já usado no refresh).
- **Mix-up entre clients**: `iss` na resposta de autorização (RFC 9207).
- **Clickjacking** da página de login: `frame-ancestors 'none'`.
- **Sessão SSO longa** contra **TOTP recente**: `auth_time` e `max_age` precisam ser
  honrados, senão uma sessão de 7 dias dispensa o segundo fator para sempre.

## Fases

1. Migration de clients + rotas de admin + discovery (sem fluxo ainda)
2. `authorize` + `token` com PKCE + `id_token` + página de login redirecionando
3. `userinfo`, `revoke`, refresh com `offline_access`
4. Sessão SSO e RP-initiated logout
5. Migrar a primeira interface (ex.: financas) e atualizar a coleção Postman

Cada fase em PR próprio. Testes de conformidade: rodar a suíte do OpenID Foundation
(`openid-conformance-suite`, perfil *Basic OP* com PKCE) contra o ambiente local ao fim da fase 4.
