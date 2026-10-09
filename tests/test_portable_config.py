"""The repo must deploy to any workspace: nothing workspace-specific in deploy config."""
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(rel):
    return yaml.safe_load((ROOT / rel).read_text())


def test_app_yaml_takes_ids_from_app_resources():
    env = {e["name"]: e for e in _load("src/app.yaml")["env"]}
    assert env["GENIE_SPACE_ID"].get("valueFrom") == "genie-space"
    assert env["LARK_APP_ID"].get("valueFrom") == "lark-app-id"
    assert env["LARK_APP_SECRET"].get("valueFrom") == "lark-app-secret"
    assert "APP_BASE_URL" not in env  # discovered at runtime


def test_bundle_pins_no_workspace_and_requires_a_genie_space():
    cfg = _load("databricks.yml")
    for target in cfg["targets"].values():
        ws = (target or {}).get("workspace") or {}
        assert "host" not in ws and "profile" not in ws
    assert "default" not in cfg["variables"]["genie_space_id"]


def test_app_resource_is_parameterized():
    app = _load("resources/lark_bot.app.yml")["resources"]["apps"]["lark_genie_bot"]
    assert app["name"] == "${var.app_name}"
    assert app["user_api_scopes"] == ["genie", "sql"]  # `genie` covers Agent mode; dashboards.genie is deprecated
    res = {r["name"]: r for r in app["resources"]}
    assert res["genie-space"]["genie_space"]["space_id"] == "${var.genie_space_id}"
    assert res["lark-app-id"]["secret"]["scope"] == "${var.secret_scope}"
    assert res["lark-app-secret"]["secret"]["scope"] == "${var.secret_scope}"


def test_no_workspace_specific_literals_in_deployable_files():
    # app URLs, workspace hosts, and hard-coded 32-hex IDs (e.g. Genie space IDs)
    pat = re.compile(r"databricksapps\.com|cloud\.databricks\.com|azuredatabricks\.net|\b[0-9a-f]{32}\b")
    files = ["src/app.yaml", "databricks.yml", "resources/lark_bot.app.yml",
             "deploy/setup_target_workspace.py", *sorted((ROOT / "src" / "app").glob("*.py"))]
    for f in files:
        path = f if isinstance(f, pathlib.Path) else ROOT / f
        assert not pat.search(path.read_text()), path


def test_setup_notebook_mirrors_the_bundle_app():
    nb = (ROOT / "deploy" / "setup_target_workspace.py").read_text()
    for needle in ('"genie"', '"sql"', 'name="genie-space"', 'name="lark-app-id"',
                   'name="lark-app-secret"', "compute_min_instances=1", "compute_max_instances=1",
                   "forward_user_access_token=True"):
        assert needle in nb, needle
