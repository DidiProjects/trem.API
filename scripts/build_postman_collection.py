"""
Builds postman/collections/trem-api.json from the app's OpenAPI, so every route is in it.

    python -m scripts.build_postman_collection

The environments in postman/environments hold baseUrl, username, password, audience and
apiKey. Login, MFA verify and refresh store the tokens in collection variables; every
request sends the Bearer, plus X-API-Key when apiKey is set (the API prefers the key).
tests/test_postman_collection.py fails when this file is out of date with the routes.
"""
import json
import re
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent.parent / "postman" / "collections" / "trem-api.json"
SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"

FOLDERS = [
    ("Health", lambda p: p == "/health"),
    ("Auth", lambda p: p in {"/auth/login", "/auth/mfa/verify", "/auth/refresh", "/auth/logout",
                             "/auth/change-password", "/.well-known/jwks.json"}),
    ("Sign-up and password reset", lambda p: p in {"/auth/username-available", "/auth/register", "/auth/verify-email",
                                                   "/auth/resend-verification", "/auth/forgot-password",
                                                   "/auth/reset-password"}),
    ("Two-factor (TOTP)", lambda p: p.startswith("/auth/mfa/")),
    ("Me", lambda p: p.startswith("/me")),
    ("Admin: users", lambda p: p.startswith("/admin/users")),
    ("Admin: applications and permissions", lambda p: p.startswith("/admin/applications")),
    ("Admin: API clients", lambda p: p.startswith("/admin/clients")),
    ("Hub catalog", lambda p: p.startswith("/hub/")),
    ("Admin: hub catalog", lambda p: p.startswith("/admin/hub/")),
    ("Login for hub services (OIDC)", lambda p: p.startswith("/oauth/") or p == "/.well-known/openid-configuration"),
    ("Users (legacy admin)", lambda p: p.startswith("/users")),
    ("PDF", lambda p: p.startswith("/pdf/")),
    ("Image", lambda p: p.startswith("/image/")),
    ("Movies", lambda p: p.startswith("/movie/")),
    ("Audio", lambda p: p.startswith("/audio/")),
    ("LLM", lambda p: p.startswith("/llm/")),
    ("Scrapers: LinkedIn", lambda p: p.startswith("/scrapers/")),
    ("Support", lambda p: p.startswith("/support/")),
]

PUBLIC = {
    "/health", "/.well-known/jwks.json", "/auth/login", "/auth/mfa/verify", "/auth/refresh",
    "/auth/logout", "/auth/username-available", "/auth/register", "/auth/verify-email", "/auth/resend-verification",
    "/auth/forgot-password", "/auth/reset-password", "/hub/services/{slug}/logo",
    # OIDC: the browser, the service's server (its own Basic credentials) or the service's token.
    "/.well-known/openid-configuration", "/oauth/authorize", "/oauth/token", "/oauth/userinfo",
}

# Field values that come from the environment or from an earlier response.
FIELD_VALUES = {
    "username": "{{username}}",
    "password": "{{password}}",
    "current_password": "{{password}}",
    "audience": "{{audience}}",
    "refresh_token": "{{refreshToken}}",
    "mfa_token": "{{mfaToken}}",
    "session": "body",
}

BODY_OVERRIDES = {
    ("POST", "/llm/v1/chat/completions"): {"messages": [{"role": "user", "content": "Olá!"}]},
    ("POST", "/llm/jobs"): {"messages": [{"role": "user", "content": "Olá!"}]},
    ("POST", "/auth/mfa/verify"): {"mfa_token": "{{mfaToken}}", "code": "", "session": "body"},
}

SAVE_TOKENS = [
    "const json = pm.response.json();",
    "if (json.mfa_required) {",
    "    pm.collectionVariables.set('mfaToken', json.mfa_token);",
    "    console.log('TOTP on: send the code with POST /auth/mfa/verify');",
    "}",
    "if (json.access_token) {",
    "    pm.collectionVariables.set('accessToken', json.access_token);",
    "    pm.collectionVariables.set('refreshToken', json.refresh_token);",
    "    pm.collectionVariables.unset('mfaToken');",
    "    console.log('Tokens saved');",
    "}",
]

CLEAR_TOKENS = [
    "if (pm.response.code === 200) {",
    "    pm.collectionVariables.unset('accessToken');",
    "    pm.collectionVariables.unset('refreshToken');",
    "    console.log('Tokens cleared');",
    "}",
]

TEST_SCRIPTS = {
    ("POST", "/auth/login"): SAVE_TOKENS,
    ("POST", "/auth/mfa/verify"): SAVE_TOKENS,
    ("POST", "/auth/refresh"): SAVE_TOKENS,
    ("POST", "/auth/logout"): CLEAR_TOKENS,
}

# Sends the API key when the environment has one; the account routes take only the login.
PRE_REQUEST = [
    "const apiKey = pm.environment.get('apiKey') || pm.collectionVariables.get('apiKey');",
    "const path = '/' + pm.request.url.getPath(true).replace(/^\\/+/, '');",
    "const accountRoute = /^\\/(auth|me|admin|users)(\\/|$)/.test(path);",
    "if (apiKey && !accountRoute) {",
    "    pm.request.headers.upsert({ key: 'X-API-Key', value: apiKey });",
    "}",
]

DESCRIPTION = """trem.API — https://api.didilv93.com

1. Import this collection and one environment from postman/environments.
2. In the environment, fill username and password (and apiKey, optionally).
3. Run Auth › POST /auth/login. With TOTP on, it stores mfaToken: put the code in
   POST /auth/mfa/verify. The access and refresh tokens are stored as collection variables.
4. Every request sends Authorization: Bearer {{accessToken}}. When apiKey is set, the tool
   routes also send X-API-Key, which the API prefers over the login.
5. The access token lasts 15 minutes: POST /auth/refresh renews it. Each refresh returns a
   new refresh token and kills the old one; the script keeps the newest.

Admin routes need a login with trem:admin proven with TOTP.

Generated by scripts/build_postman_collection.py from the OpenAPI; regenerate it after
changing routes."""


def _resolve(schema: dict, components: dict) -> dict:
    while "$ref" in schema:
        schema = components[schema["$ref"].rsplit("/", 1)[1]]
    if "anyOf" in schema:
        options = [s for s in schema["anyOf"] if s.get("type") != "null"]
        merged = {k: v for k, v in schema.items() if k != "anyOf"}
        return {**_resolve(options[0], components), **merged} if options else merged
    return schema


def _is_file(schema: dict) -> bool:
    return schema.get("format") == "binary" or "contentMediaType" in schema


def _example(name: str, schema: dict, components: dict):
    if name in FIELD_VALUES:
        return FIELD_VALUES[name]
    schema = _resolve(schema, components)
    if "default" in schema:
        return schema["default"]
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        return _object_example(schema, components)
    if kind == "array":
        return []
    if kind == "integer":
        return schema.get("minimum", 0)
    if kind == "number":
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    return ""


def _object_example(schema: dict, components: dict) -> dict:
    return {name: _example(name, prop, components) for name, prop in schema.get("properties", {}).items()}


def _form_value(value) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _body(method: str, path: str, operation: dict, components: dict):
    if (method, path) in BODY_OVERRIDES:
        return _raw_json(BODY_OVERRIDES[(method, path)])
    content = (operation.get("requestBody") or {}).get("content", {})
    if "application/json" in content:
        schema = _resolve(content["application/json"]["schema"], components)
        return _raw_json(_object_example(schema, components))
    for mode, media in (("formdata", "multipart/form-data"), ("urlencoded", "application/x-www-form-urlencoded")):
        if media not in content:
            continue
        schema = _resolve(content[media]["schema"], components)
        required = set(schema.get("required", []))
        fields = []
        for name, prop in schema.get("properties", {}).items():
            prop = _resolve(prop, components)
            items = _resolve(prop.get("items", {}), components)
            field = {"key": name}
            if mode == "formdata" and (_is_file(prop) or _is_file(items)):
                field.update(type="file", src=[] if prop.get("type") == "array" else "")
            else:
                field.update(type="text", value=_form_value(_example(name, prop, components)))
            if prop.get("description"):
                field["description"] = prop["description"]
            if name not in required:
                field["disabled"] = True
            fields.append(field)
        return {"mode": mode, mode: fields}
    if "/parts/" in path:
        return {"mode": "file", "file": {"src": ""}}
    return None


def _raw_json(value) -> dict:
    return {
        "mode": "raw",
        "raw": json.dumps(value, indent=2, ensure_ascii=False),
        "options": {"raw": {"language": "json"}},
    }


def _postman_path(path: str) -> str:
    return re.sub(r"\{(\w+)\}", r":\1", path)


def _url(path: str, operation: dict) -> dict:
    postman_path = _postman_path(path)
    segments = [s for s in postman_path.split("/") if s]
    url = {"raw": "{{baseUrl}}" + postman_path, "host": ["{{baseUrl}}"], "path": segments}
    path_params = [p for p in operation.get("parameters", []) if p["in"] == "path"]
    if path_params:
        url["variable"] = [{"key": p["name"], "value": ""} for p in path_params]
    query = []
    for p in operation.get("parameters", []):
        if p["in"] != "query":
            continue
        schema = p.get("schema", {})
        value = schema.get("default", "")
        query.append({"key": p["name"], "value": "" if value is None else str(value),
                      "disabled": not p.get("required", False)})
    if query:
        url["query"] = query
        enabled = [f"{q['key']}={q['value']}" for q in query if not q["disabled"]]
        if enabled:
            url["raw"] += "?" + "&".join(enabled)
    return url


def _item(method: str, path: str, operation: dict, components: dict) -> dict:
    request = {"method": method}
    if path in PUBLIC:
        request["auth"] = {"type": "noauth"}
    body = _body(method, path, operation, components)
    if body:
        if body["mode"] == "raw":
            request["header"] = [{"key": "Content-Type", "value": "application/json"}]
        request["body"] = body
    request["url"] = _url(path, operation)
    description = "\n\n".join(x for x in (operation.get("summary"), operation.get("description")) if x)
    if description:
        request["description"] = description.strip()

    item = {"name": f"{method} {_postman_path(path)}"}
    if (method, path) in TEST_SCRIPTS:
        item["event"] = [{"listen": "test", "script": {"type": "text/javascript",
                                                       "exec": TEST_SCRIPTS[(method, path)]}}]
    item["request"] = request
    return item


def build(openapi: dict) -> dict:
    components = openapi.get("components", {}).get("schemas", {})
    folders = {name: [] for name, _ in FOLDERS}
    for path, operations in openapi["paths"].items():
        folder = next((name for name, match in FOLDERS if match(path)), None)
        if folder is None:
            raise SystemExit(f"no Postman folder for {path}: add it to FOLDERS")
        for method, operation in operations.items():
            folders[folder].append(_item(method.upper(), path, operation, components))

    return {
        "info": {"name": "trem.API", "description": DESCRIPTION, "schema": SCHEMA},
        "variable": [
            {"key": "accessToken", "value": ""},
            {"key": "refreshToken", "value": ""},
            {"key": "mfaToken", "value": ""},
        ],
        "auth": {"type": "bearer", "bearer": [{"key": "token", "value": "{{accessToken}}", "type": "string"}]},
        "event": [{"listen": "prerequest", "script": {"type": "text/javascript", "exec": PRE_REQUEST}}],
        "item": [{"name": name, "item": items} for name, items in folders.items() if items],
    }


def render(openapi: dict) -> str:
    return json.dumps(build(openapi), indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    from app.main import app

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render(app.openapi()), encoding="utf-8", newline="\n")
    print(f"written {OUTPUT}")


if __name__ == "__main__":
    main()
