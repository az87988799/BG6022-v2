# P7 local usage

## Direct terminal entry

`start_chat.cmd` is the single interactive entry point. From
`E:\BG6022-v2`, copy `.env.example` to the repository root as `.env` and fill
in `DEEPSEEK_API_KEY`, the current DeepSeek model, and the exact local ORCA
path/version. The real key stays local and is never written to a request file,
state record, log, or commit.

```powershell
Copy-Item .env.example .env
# edit .env: DEEPSEEK_API_KEY=... and BG6022_ORCA_EXECUTABLE=...
.\start_chat.cmd
```

The default profile is `real`: DeepSeek interprets the request, PubChem
resolves names/CAS/CID, RDKit handles local SMILES, and the existing local P5
ORCA backend executes the fixed Opt → Freq → independent SP chain after each
explicit approval. Startup does not run a paid model probe or an ORCA smoke
job. Run the explicit readiness check before a real calculation:

```powershell
.venv\Scripts\python.exe scripts\start_chat.py --doctor
```

The launcher uses the fixed state root `.tmp\p7\real-chat`, resumes the last
valid conversation, and keeps a single OS-level lock. It does not install
packages, pull code, or silently approve a plan, identity, or P5 node. If the
real ORCA profile is not ready, planning may still be inspected but no
executable approval token is issued.

For local testing, profiles are explicit:

- `--profile offline` uses the deterministic baseline planner and fake P4/P5/P6
  chain; it makes no network or model calls.
- `--profile deepseek_fake` uses the real DeepSeek adapter with fake identity
  and execution, so it still requires a real DeepSeek key and network access.

Inside the window, ordinary Chinese or English text is sent to the shared
`P7ChatDriver`. Use `/help`, `/status`, `/tasks`, `/use <任务>`, `/new`,
`/resume <id>`, `/reconcile <任务>`, `/accept <token>`, `/reject <token>`, and
`/exit`. Every approval is explicit; the driver only performs bounded worker
progress while idle and after input. Default output is human-readable. Add
`--json` when a JSON-lines terminal protocol is needed.

The same driver is used by the explicit CLI entry:

```powershell
.venv\Scripts\python.exe -m orca_agent --state-root .tmp\p7\chat agent-chat `
  --new-conversation --profile offline
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
  --new-conversation --profile offline --json | ConvertFrom-Json
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
`approve_execution` token in turn. The fixed P5 protocol has three nodes; a
plan/identity acceptance never substitutes for those three execution grants.

The P7 adapter reads `.env` from the current project directory. Process
environment variables with the same names take precedence. The `.env` file is
ignored by Git and must remain local. The recommended model example is
`deepseek-v4-flash`; change `BG6022_P7_MODEL` when the provider exposes a
different current model.

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

`agent-work --profile real` uses the frozen real profile only after the
configuration has passed the readiness checks. It does not auto-approve a P5
action and must not be used with an unknown ORCA configuration. Prefer the
profile flags over mixing `--planner`, `--backend`, and `--allow-real-orca`;
incompatible combinations are rejected. No key belongs in a request file or
repository.
