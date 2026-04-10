# RingPing

RingPing is a starter desktop app for routing RingCentral thread requests into local `codex exec` runs, isolating each request in its own git worktree, and giving you a per-project review/push dashboard.

## What this starter already does

- Loads project definitions from JSON.
- Shows a desktop dashboard grouped by project.
- Accepts manual requests now, with a local webhook endpoint ready for RingCentral intake.
- Polls RingCentral Team Messaging directly for new posts, which is better suited to a desktop install than requiring a public webhook.
- Runs each request through local `codex exec`.
- Uses isolated git worktrees so one request does not trample another.
- Supports project-level auto-push.
- Lets you review a request's worktree, inspect its diff, and push when ready.
- Downloads file attachments from RingCentral `fix:` posts into a root-level `.ringping_artifacts` folder inside the request worktree so Codex can use them during the fix.

## What is intentionally still thin

- RingCentral auth/subscription setup is scaffolded, but not auto-provisioned from the UI yet.
- Status messages back into RingCentral are optional and not the primary workflow yet.
- Retry currently reuses the same worktree branch instead of starting a fresh branch.
- No packaging or installer yet.

## Runtime prerequisites

1. Install Python 3.11 or newer.
2. Make sure `codex` is installed and logged in with ChatGPT or an API key.
3. Make sure each target project repo already exists locally.
4. Copy `.env.example` to `.env`.
5. Copy `config/projects.example.json` to `config/projects.json` and update repo paths/chat IDs.
6. Set `RINGPING_RINGCENTRAL_COMMAND_PREFIX=fix:` so only explicit fix requests create code tasks.

## Run

```powershell
py -3.11 -m pip install -e .
py -3.11 -m ringping.app
```

If `python` is already on your PATH:

```powershell
python -m pip install -e .
python -m ringping.app
```

## How the workflow works

1. A request arrives manually or through the webhook.
   RingCentral polling is also available and is the easier path for a local desktop setup.
   In your current setup, only posts starting with `fix:` are ingested from the RingCentral team chat.
2. RingPing maps it to a project by chat ID.
3. RingPing creates a git worktree and branch for that request.
4. RingPing downloads any attached PDFs, spreadsheets, or other files into `.ringping_artifacts/request-<id>/` inside the worktree.
5. RingPing runs `codex exec` inside the worktree.
6. If the project has `auto_push` enabled, RingPing commits and pushes automatically.
7. Otherwise the request lands in `ready`, and you can review then push from the dashboard.

## Push behavior

Per project, `push_mode` can be:

- `branch`: safer default. Pushes the request branch to the remote.
- `direct`: pushes `HEAD` back onto the configured `base_branch`.

Use `direct` only if you are comfortable with unattended release pushes.

## RingCentral setup

See `docs/ringcentral-setup.md`.
