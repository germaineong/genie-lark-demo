# Deploy without the CLI (Databricks UI)

Deploy the bot to a workspace where you can't use the Databricks CLI. Everything below
happens in the workspace UI, under your own login. (With CLI access, the README's
bundle deploy is simpler.)

Nothing in the repo needs editing: `src/app.yaml` reads the Genie space and the Lark
credentials from **app resources**, and the app discovers its own URL at runtime.

## What you need

- A **Genie space** to answer from, with **CAN MANAGE** for you. Its ID is the `<ID>`
  in the space's URL `/genie/rooms/<ID>`.
- A **Lark/Feishu bot** and its App ID + App Secret. Set it up as in the README's
  [Set up the Lark bot](../README.md#set-up-the-lark-bot).
- **One deployment per Lark bot.** If this Lark bot is already served by another
  deployment, stop that one first (a bot allows one WebSocket connection).

## Steps

1. **Get the code into the workspace.** Workspace → Create → **Git folder** → paste the
   GitHub repo URL. (Or import the repo files.) The app's source is the repo's `src/`
   folder.

2. **Run the setup notebook.** Open `deploy/setup_target_workspace.py` from the Git
   folder (it's a Databricks notebook). Fill cell 0 — Lark App ID and Secret, Genie
   space ID, and `SOURCE_PATH` (the Git folder's `src/`) — then run **cell 1** to create
   the secret scope and store the credentials. Clear the secret from cell 0 afterwards.

3. **Create the app** — either:
   - **Notebook cell 2:** creates and deploys the app with everything below already set; or
   - **Apps UI:** create a custom app (e.g. `lark-genie-bot`), then set:
     - **Source code:** the Git folder's `src/`
     - **User authorization scopes:** `genie`, `sql`
     - **Resources** — the names must match exactly, because `app.yaml` reads them:

       | Resource name | Type | Value | Permission |
       |---|---|---|---|
       | `genie-space` | Genie space | your Genie space | Can view |
       | `lark-app-id` | Secret | scope `lark_bot`, key `lark_app_id` | Can read |
       | `lark-app-secret` | Secret | scope `lark_bot`, key `lark_app_secret` | Can read |

     - **Compute:** min = max = **1 instance** (the in-memory token map must not be split)

     …then **Deploy** from `src/`.

4. **Open the app URL once** in your browser. That confirms it's running, and the bot
   learns its public URL from the visit if it can't read it from the Apps API.

5. **Give people access:**
   - **CAN USE** on the app for the people (or a group) who'll use the bot.
   - **CAN RUN** on the Genie space, `SELECT` on its tables, and **CAN USE** on its SQL
     warehouse. OBO runs Genie as each user, so this is what gates their data.

6. **Verify.** In Lark, DM the bot **`绑定`** → open the link → ask a question → confirm
   in the query history that it ran as you.

## Notes

- `DATABRICKS_HOST` is injected by the Apps runtime, so the per-user client and the
  「在Genie中查看」 button point at this workspace automatically.
- Optional `app.yaml` knobs: `LARK_REGION` (`cn` for Feishu), `BOT_TITLE` (card header), `GENIE_MODE` (`chat` to skip Agent mode),
  and `APP_BASE_URL` (only if URL discovery can't work in your setup).
- If your Apps UI doesn't show user-authorization scopes, use the notebook (cell 2),
  which sets them via the SDK.
- To update later: pull the Git folder, then redeploy the app from `src/`.
