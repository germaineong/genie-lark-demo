# Deploy without the CLI (Databricks UI)

Deploy the bot to a workspace where you can't use the Databricks CLI. Everything below
happens in the workspace UI, under your own login. (With CLI access, the README's
bundle deploy is simpler.)

Nothing in the repo needs editing (on Feishu, change `LARK_REGION` to `"cn"` in
`src/app.yaml`): it reads the Genie space and the Lark credentials from **app
resources**, and the app discovers its own URL at runtime.

## What you need

- A **Genie space** to answer from, with **CAN MANAGE** for you. Its ID is the `<ID>`
  in the space's URL `/genie/rooms/<ID>`.
- A **Lark/Feishu bot** and its App ID + App Secret. Set it up as in the README's
  [Set up the Lark bot](../README.md#set-up-the-lark-bot): steps 1–4 now, steps 5–6 once
  the app is running.
- **Apps may use the `genie` and `sql` scopes.** Admins can restrict them under Settings →
  Development → Apps → *Restrict OAuth scopes for apps to selected values*.
- **Outbound network access** from the app to Lark (`*.larksuite.com` or `*.feishu.cn`)
  and PyPI, if your workspace restricts serverless egress.
- **One deployment per Lark bot.** If another deployment already uses this Lark app,
  stop it first: Lark delivers each message to only one connection.

## Steps

1. **Get the code into the workspace.** Workspace → Create → **Git folder** → paste the
   GitHub repo URL. (Or import the repo files.) The app's source is the repo's `src/`
   folder.

2. **Run the setup notebook.** Open `deploy/setup_target_workspace.py` from the Git
   folder (it's a Databricks notebook).
   - In cell 0, set the Genie space ID and `SOURCE_PATH` (the Git folder's `src/`), then
     run it. It adds two widgets at the top of the notebook.
   - Type the Lark **App ID** and **App Secret** into those widgets, not into the code:
     widget values aren't saved in the notebook file.
   - Run **cell 1**. It creates the secret scope, stores the credentials, and removes
     the App Secret widget.

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

     - **Compute:** leave horizontal scaling off, so it runs one instance (the in-memory
       token map must not be split)

     …then **Deploy** from `src/`.

4. **Open the app URL once** in your browser. That confirms it's running, and the bot
   learns its public URL from the visit if it can't read it from the Apps API.

5. **Give people access:**
   - **CAN USE** on the app for the people (or a group) who'll use the bot.
   - **CAN RUN** on the Genie space; `USE CATALOG` and `USE SCHEMA` on its catalog and
     schema plus `SELECT` on its tables; and **CAN USE** on its SQL warehouse. OBO runs
     Genie as each user, so this is what gates their data.

6. **Finish the Lark setup:** the README's Lark steps 5–6 (long connection, then
   publish). Lark only saves the long-connection setting while the app is running.

7. **Verify.** In Lark, DM the bot **`绑定`** → open the link → ask a question → confirm
   in the query history that it ran as you. If something's off, see the README's
   [Troubleshooting](../README.md#troubleshooting).

## Notes

- `DATABRICKS_HOST` is injected by the Apps runtime, so the per-user client and the
  「在Genie中查看」 button point at this workspace automatically.
- Optional `app.yaml` knobs: `LARK_REGION` (`cn` for Feishu), `BOT_TITLE` (card header), `GENIE_MODE` (`chat` to skip Agent mode),
  and `APP_BASE_URL` (only if URL discovery can't work in your setup).
- If you can't add the `genie` / `sql` user-authorization scopes, or the app won't
  start because of them, a workspace admin has restricted app scopes (see
  [What you need](#what-you-need)).
- To update later: pull the Git folder, then redeploy the app from `src/`.
