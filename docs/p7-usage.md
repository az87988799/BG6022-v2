# P7 local usage

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
