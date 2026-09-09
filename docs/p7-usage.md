# P7 local usage

## Direct terminal entry

From `E:\BG6022-v2`, copy `.env.example` to the repository root as `.env` and
fill in `DEEPSEEK_API_KEY` and `BG6022_P7_MODEL`. The real key stays local and
is never written to a request file or committed.

Double-click `start_chat.cmd`, or run:

```powershell
.venv\Scripts\python.exe scripts\start_chat.py
```

The launcher uses the fixed state root `.tmp\p7\chat`, resumes the last valid
conversation, and keeps a single OS-level lock. It does not install packages,
pull code, enable real ORCA, or approve a plan/identity/P5 node. The default
planner is the configured DeepSeek model with the fake calculation backend;
use `--planner baseline` for an entirely offline smoke run.

Inside the window, ordinary Chinese or English text is sent to the shared
`P7ChatDriver`. Use `/help`, `/status`, `/tasks`, `/new`, `/resume <id>`,
`/accept <token>`, `/reject <token>`, and `/exit`. Every approval is explicit;
the driver only performs bounded worker progress while idle and after input.
Default output is human-readable. Add `--json` when a JSON-lines terminal
protocol is needed.

The same driver is used by the explicit CLI entry:

```powershell
.venv\Scripts\python.exe -m orca_agent --state-root .tmp\p7\chat agent-chat `
  --new-conversation --planner baseline
```

For a display-only query, the immutable `delivery` in the response is the
original scientific DeliveryRecord. A changed layout/precision/quantity is
returned in `view` as a separately hashed RenderedResultView.

The P7 interface is deliberately separate from the legacy `start`, `prepare`,
and `worker` commands. Run from the repository root with the project Python
environment active.

```powershell
$P7Root = "E:\BG6022-v2\.tmp\p7-demo"
$chat = python -m orca_agent --state-root $P7Root agent-chat `
  --new-conversation --planner baseline --json | ConvertFrom-Json
$Conversation = $chat.conversation_id

$proposal = python -m orca_agent --state-root $P7Root agent-message `
  --conversation $Conversation `
  --text "calculate SMILES CCO charge 0 multiplicity 1 independent single point energy" `
  --save-request "$P7Root\message.json" --json | ConvertFrom-Json
$PlanToken = $proposal.response.pending_actions[0].token

python -m orca_agent --state-root $P7Root agent-action `
  --conversation $Conversation --token $PlanToken --decision accept `
  --save-request "$P7Root\accept-plan.json" --json

python -m orca_agent --state-root $P7Root agent-work `
  --conversation $Conversation --max-effects 16 --max-seconds 30 --json
```

The work response exposes the P4 identity token. Accept it with
`agent-action`, run `agent-work` again, and accept each displayed
`approve_execution` token in turn. The P5 fake protocol has three nodes; a
plan/identity acceptance never substitutes for those three execution grants.

For a local DeepSeek setup, copy the repository template before starting the
CLI and fill in the two values:

```powershell
Copy-Item .env.example .env
# edit .env: DEEPSEEK_API_KEY=... and BG6022_P7_MODEL=...
```

The P7 adapter reads `.env` from the current project directory. Process
environment variables with the same names take precedence. The `.env` file is
ignored by Git and must remain local.

After the final bounded work call:

```powershell
'{"kind":"result"}' | Set-Content -Encoding utf8 "$P7Root\query.json"
python -m orca_agent --state-root $P7Root agent-query `
  --conversation $Conversation --request-json "$P7Root\query.json" --json
python -m orca_agent --state-root $P7Root agent-export `
  --conversation $Conversation --format md --output "$P7Root\result.md"
python -m orca_agent --state-root $P7Root agent-verify `
  --conversation $Conversation --json
```

`agent-work --allow-real-orca` only enables the configured real backend. It does
not auto-approve a P5 action and must not be used with an unknown ORCA
configuration. DeepSeek requires `--planner deepseek_chat --allow-llm` and the
environment variables named in the implementation plan, either from `.env` or
the process environment; no key belongs in a request file or repository.
