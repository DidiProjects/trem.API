# OIDC no trem.API — plano

Status: **fases 1 e 2 implementadas** (clients e provedor). O resto está descrito abaixo, na ordem de entrega.

## Por que

Os serviços do servidor (Jellyfin, Seerr) têm login próprio: cada pessoa teria mais uma
senha, sem o MFA do trem.API. Eles aceitam login de fora por OpenID Connect (Jellyfin pelo
plugin de SSO, Seerr pela imagem `preview-new-oidc`), e por cabeçalho de proxy nenhum dos
dois aceita. Com o trem.API como provedor OIDC, quem tem `hub:<slug>` entra no serviço com
a própria conta, e a senha e o TOTP nunca passam pelo serviço.

Sonarr e Radarr ficam de fora: não têm usuários, só um admin.

## Desenho

A tela de login é a do **trem.FRONT**, que já tem a sessão. O refresh cookie do trem.API é
`Partitioned`: guardado sob o site do trem.FRONT, ele não aparece quando o navegador abre o
trem.API como site de topo. Por isso `/oauth/authorize` só valida e guarda o pedido, e quem
aprova é o trem.FRONT, com o access token que ele já tem:

```
serviço → GET  /oauth/authorize?client_id&redirect_uri&state&nonce&code_challenge…
        → 302 trem.FRONT /sso?request=<id>            ("Entrar no Didiflix como diego")
front   → POST /oauth/authorize/{id}/approve  (Bearer) → { redirect: <redirect_uri>?code&state&iss }
serviço → POST /oauth/token (client_secret + code_verifier) → id_token + access_token
serviço → GET  /oauth/userinfo
```

O pedido não fica preso ao navegador que o abriu (o cookie para isso seria particionado).
Um link de login forjado é barrado pelo `state` e pelo PKCE do lado do serviço, que é o
papel deles no protocolo, e a tela do trem.FRONT mostra o nome do serviço antes do clique.

## O que já existe e é reaproveitado

| Peça | Onde | Uso no OIDC |
|---|---|---|
| Chave RS256 com `kid` (RFC 7638) e rotação | `app/core/security.py` | assina o `id_token` |
| `GET /.well-known/jwks.json` | `routers/auth.py` | `jwks_uri` do discovery |
| `iss` configurável (`JWT_ISSUER`) | `config.py` | `issuer` do discovery |
| Serviços do hub e `hub:<slug>` | `hub_services`, `permissions` | cada serviço é um client; `hub:<slug>` decide quem entra |
| Admin do trem.API (`trem:admin`) | `permissions` | vira o grupo `admin` no token: o serviço dá admin a quem é admin aqui |

## Fases

### 1. Clients (feito)

Tabela `oidc_clients`, um por serviço do hub (`client_id` = `hub.<slug>`, some com o serviço).
Rotas em `/admin/hub/services/{slug}/oidc`: criar ou trocar as redirect URIs, rotacionar o
segredo, remover. Segredo de 256 bits, mostrado uma vez, guardado como SHA-256 (como as
API keys: hash lento só importa para segredo escolhido por gente). Redirect URIs absolutas,
sem usuário, senha nem fragmento, até dez. `http` só para endereço local, privado ou da
Tailscale (100.64.0.0/10), onde o código não cruza a internet; qualquer outro host exige
`https`.

### 2. Provedor (feito)

| Endpoint | Função |
|---|---|
| `GET /.well-known/openid-configuration` | discovery |
| `GET /oauth/authorize` | valida `client_id`, `redirect_uri` (exata), `response_type=code`, `scope` com `openid`, `state`, `nonce`, `code_challenge` S256; guarda o pedido (vida curta) e manda para o trem.FRONT |
| `GET /oauth/authorize/{id}` | o pedido para a tela do trem.FRONT: nome e logo do serviço (Bearer) |
| `POST /oauth/authorize/{id}/approve` | Bearer; exige `hub:<slug>`; gera o código e devolve o redirect |
| `POST /oauth/authorize/{id}/deny` | Bearer; volta ao serviço com `access_denied` |
| `POST /oauth/token` | `authorization_code` + `code_verifier`, client autenticado por `client_secret_basic` ou `client_secret_post` |
| `GET /oauth/userinfo` | claims do usuário para o access token emitido no `/oauth/token` |

Código de autorização: 256 bits, guardado como SHA-256, uso único, 60 s, amarrado a
client, `redirect_uri`, usuário, `nonce`, `code_challenge`, `amr`. Erro do pedido com
`redirect_uri` válida volta para o serviço (`error`, `state`, `iss`, RFC 9207); com
`client_id` ou `redirect_uri` inválidos, nunca redireciona.

Um código gasto (inclusive por uma troca que falhou) não vale de novo. O RFC 6749 sugere
revogar os tokens já emitidos quando um código é reapresentado; aqui eles são JWT sem
estado e duram pouco (access token 15 min, id_token 5 min), e o userinfo relê a conta a
cada chamada.

Biblioteca: PyJWT, sem Authlib. O servidor do Authlib não tem integração com
Starlette/FastAPI, e o recorte (um grant, clients confidenciais) é pequeno; cada caso de
erro dos RFCs tem teste em `tests/test_oidc_provider.py`.

`id_token`: `iss`, `sub` (id do usuário), `aud` (= `client_id`), `exp`, `iat`, `nonce`,
`amr`, `azp`, `preferred_username`, `name`, `email` (scope `email`) e `groups` (`admin`
para quem tem `trem:admin`). Assinado com a chave atual; `kid` no header.

### 3. trem.FRONT

Página `/sso`: entra se preciso, mostra "Entrar no <serviço> como <usuário>", aprova e
segue para o serviço. Admin do serviço ganha a seção do login único (URIs, segredo).

### 4. Servidor

Jellyfin com o plugin de SSO apontando para o trem.API (usuários casados pelo
`preferred_username`, admin pelo grupo `admin`), Quick Connect para as TVs. Depois o
Seerr na imagem `preview-new-oidc`.

## Sem a Tailscale, um dia

O plano é acessar a máquina direto, sem a Tailscale. Nada do fluxo depende dela: os
redirecionamentos passam pelo navegador e o trem.API nunca chama o serviço; só o serviço
chama o trem.API (`/oauth/token`, JWKS), pelo endereço público, que é o `iss`. A mudança
fica na configuração:

1. Publicar o serviço com `https` no próprio domínio (proxy reverso com certificado). O
   Jellyfin também precisa de "Known proxies" para enxergar o `https`.
2. Acrescentar a redirect URI `https://…` ao client, mantendo a da Tailscale durante a
   transição (até dez por client), e tirar a antiga depois.
3. No serviço, trocar o endereço público ("External URL" no Seerr) para o novo.

`http` para host público é recusado justamente para essa hora: exposto direto, o código de
login trafegaria aberto pela internet.

## Fora, por ora

Refresh token e `offline_access`, `revoke`, logout único (sair do trem.API não derruba a
sessão do serviço), `prompt=login` e `max_age`, consentimento por escopo, clients
públicos.

## Riscos

- **Open redirect** em `redirect_uri`: comparação exata e teste dedicado.
- **Código vazado**: uso único, 60 s, PKCE e segredo do client.
- **Mix-up entre clients**: `iss` na resposta de autorização.
- **Pedido aprovado por quem não o abriu**: `state` e PKCE no serviço; nome do serviço na
  tela de aprovação.
- **Clickjacking** da tela `/sso`: `frame-ancestors 'none'` no trem.FRONT.
