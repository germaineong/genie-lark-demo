# Databricks notebook source
# MAGIC %md
# MAGIC # Set up the Lark → Genie OBO bot in this workspace (no CLI needed)
# MAGIC
# MAGIC Run this **in the workspace you're deploying to, under your own login**. It covers
# MAGIC what the point-and-click UI can't:
# MAGIC 1. **(Required)** create the secret scope and store the Lark credentials.
# MAGIC 2. **(Optional)** create + deploy the App via the SDK, with the same config as the
# MAGIC    bundle (`resources/lark_bot.app.yml`). Or do this step in the Apps UI instead.
# MAGIC
# MAGIC Prereq: the repo is in this workspace (e.g. a **Git folder** cloned from GitHub) and
# MAGIC `SOURCE_PATH` points at its `src/` folder. `src/app.yaml` needs **no edits** (except
# MAGIC `LARK_REGION` → `"cn"` for Feishu): the Genie space and Lark credentials are bound as
# MAGIC app resources, and the app discovers its own URL at runtime.

# COMMAND ----------
# MAGIC %md
# MAGIC ## 0. Config — fill these in
# MAGIC Run this cell once: it adds two widgets at the top of the notebook. Type the Lark
# MAGIC **App ID** and **App Secret** into them, not into the code — widget values aren't
# MAGIC saved in the notebook file, so the secret can't be committed with the Git folder.

# COMMAND ----------
dbutils.widgets.text("lark_app_id", "", "Lark App ID")
dbutils.widgets.text("lark_app_secret", "", "Lark App Secret")
GENIE_SPACE_ID = ""     # the Genie space to answer from: the <ID> in /genie/rooms/<ID>
APP_NAME = "lark-genie-bot"  # lowercase letters, numbers, hyphens; unique per workspace
SECRET_SCOPE = "lark_bot"
# Workspace path of the repo's src/ folder (contains app.yaml, requirements.txt, app/):
SOURCE_PATH = "/Workspace/Users/<you>/genie-lark-demo/src"

# COMMAND ----------
# MAGIC %md
# MAGIC ## 1. Secret scope + Lark credentials  (REQUIRED)
# MAGIC Secret values can't be stored from the UI — this cell creates the scope (if needed)
# MAGIC and stores the credentials under your identity. It then removes the App Secret
# MAGIC widget; to re-run it, run cell 0 again first.

# COMMAND ----------
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
LARK_APP_ID = dbutils.widgets.get("lark_app_id").strip()
LARK_APP_SECRET = dbutils.widgets.get("lark_app_secret").strip()
assert LARK_APP_ID and LARK_APP_SECRET, "Type the Lark App ID and App Secret into the widgets at the top first."

try:
    w.secrets.create_scope(scope=SECRET_SCOPE)
    print("created scope:", SECRET_SCOPE)
except Exception as e:
    print("scope exists or restricted (ok if it already exists):", e)

w.secrets.put_secret(scope=SECRET_SCOPE, key="lark_app_id", string_value=LARK_APP_ID)
w.secrets.put_secret(scope=SECRET_SCOPE, key="lark_app_secret", string_value=LARK_APP_SECRET)
print(f"secrets in {SECRET_SCOPE}:", [s.key for s in w.secrets.list_secrets(scope=SECRET_SCOPE)])
dbutils.widgets.remove("lark_app_secret")  # stored now; don't leave it on screen
print("Done — the App Secret widget has been removed.")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 2. Create + deploy the App  (OPTIONAL — or do this in the Apps UI)
# MAGIC Mirrors `resources/lark_bot.app.yml`: OBO user scopes, plus the Genie space and two
# MAGIC Lark secret resources. The app runs as one instance (the default — don't enable
# MAGIC horizontal scaling). Creating provisions compute (a few minutes), then the deploy
# MAGIC ships the code at `SOURCE_PATH`. Safe to re-run: if the app exists, it only deploys.

# COMMAND ----------
from databricks.sdk.errors import NotFound
from databricks.sdk.service.apps import (
    App,
    AppDeployment,
    AppResource,
    AppResourceGenieSpace,
    AppResourceGenieSpaceGenieSpacePermission,
    AppResourceSecret,
    AppResourceSecretSecretPermission,
)

assert GENIE_SPACE_ID, "Fill GENIE_SPACE_ID in cell 0 first."
assert "<you>" not in SOURCE_PATH, "Set SOURCE_PATH in cell 0 to your Git folder's src/ path."
w.workspace.get_status(f"{SOURCE_PATH}/app.yaml")  # fails here if SOURCE_PATH is wrong
READ = AppResourceSecretSecretPermission.READ
app = App(
    name=APP_NAME,
    description="Lark → Genie bot — per-user OBO",
    default_source_code_path=SOURCE_PATH,
    user_api_scopes=["genie", "sql"],
    resources=[
        AppResource(name="genie-space",
                    genie_space=AppResourceGenieSpace(
                        name="Genie space", space_id=GENIE_SPACE_ID,
                        permission=AppResourceGenieSpaceGenieSpacePermission.CAN_VIEW)),
        AppResource(name="lark-app-id",
                    secret=AppResourceSecret(scope=SECRET_SCOPE, key="lark_app_id", permission=READ)),
        AppResource(name="lark-app-secret",
                    secret=AppResourceSecret(scope=SECRET_SCOPE, key="lark_app_secret", permission=READ)),
    ],
)

try:
    w.apps.get(name=APP_NAME)
    print(f"{APP_NAME} already exists — deploying the latest code")
except NotFound:
    w.apps.create_and_wait(app=app)
w.apps.deploy_and_wait(app_name=APP_NAME, app_deployment=AppDeployment(source_code_path=SOURCE_PATH))
print("App URL:", w.apps.get(name=APP_NAME).url)

# COMMAND ----------
# MAGIC %md
# MAGIC ## 3. Finish
# MAGIC 1. **Open the App URL** above once in your browser. That confirms the app is up, and
# MAGIC    the bot learns its public URL from the visit if it can't read it from the Apps API.
# MAGIC 2. **Share the app:** give the people who'll use the bot **CAN USE** on the app
# MAGIC    (Apps → this app → Permissions). They open the bind page through it.
# MAGIC 3. **Grant data access:** **CAN RUN** on the Genie space; `USE CATALOG` and
# MAGIC    `USE SCHEMA` on its catalog and schema plus `SELECT` on its tables; and **CAN USE**
# MAGIC    on its SQL warehouse. OBO runs Genie as each user, so this is what gates their data.
# MAGIC 4. **One deployment per Lark bot:** if another deployment already serves this Lark
# MAGIC    bot, stop it (a bot allows one WebSocket connection).
# MAGIC 5. In Lark, DM the bot **`绑定`**, open the link, then ask a question.
