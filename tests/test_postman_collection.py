"""The committed Postman collection must match the routes; regenerate it with scripts.build_postman_collection."""
import json
from pathlib import Path

from app.main import app
from scripts.build_postman_collection import OUTPUT, PUBLIC, render

ROOT = Path(__file__).resolve().parent.parent


def _requests(collection):
    for folder in collection["item"]:
        yield from folder["item"]


def test_committed_collection_is_up_to_date():
    assert OUTPUT.read_text(encoding="utf-8") == render(app.openapi()), (
        "postman/collections/trem-api.json is stale: run python -m scripts.build_postman_collection"
    )


def test_every_route_is_in_the_collection():
    collection = json.loads(OUTPUT.read_text(encoding="utf-8"))
    names = {item["name"] for item in _requests(collection)}
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            postman_path = path.replace("{", ":").replace("}", "")
            assert f"{method.upper()} {postman_path}" in names


def test_only_public_routes_skip_the_bearer():
    collection = json.loads(OUTPUT.read_text(encoding="utf-8"))
    for item in _requests(collection):
        public = item["request"].get("auth", {}).get("type") == "noauth"
        path = "/" + "/".join(item["request"]["url"]["path"])
        assert public == (path.replace(":", "{") in {p.replace("}", "") for p in PUBLIC} or path in PUBLIC)


def test_environments_carry_no_values_for_secrets():
    for env_file in (ROOT / "postman" / "environments").glob("*.json"):
        values = {v["key"]: v["value"] for v in json.loads(env_file.read_text(encoding="utf-8"))["values"]}
        assert values["baseUrl"]
        assert values["accessToken"] == "" and values["apiKey"] == ""
