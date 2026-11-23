# nvwabase — The Nüwa Foundation

<p align="center"><img src="docs/assets/three-capabilities.jpg" alt="Three capabilities: Create / Language / History" width="720"></p>

> The ground on which AIs grow into civilizations: a **layered fact-intent
> graph** as shared working memory for long-horizon multi-agent work.
> Pure stdlib, zero dependencies, opt-in per project — many agents share one
> stele; individuals die, the civilization persists.

**English** | [中文](README.zh-CN.md)

📖 **[Full design article (WeChat)](https://mp.weixin.qq.com/s/nibHYWmDTf6TkNhPhtvFdw)**

This fork is a **general-purpose** assistant base. It keeps the shared
fact-intent graph, project hooks, dispatch loop, and `nvwa` tmux runtime,
and it can drive Claude Code, OpenAI Codex, or the xAI Grok CLI. Domain
playbooks are not part of the tree. See [What was removed](#removed).

---

## 1. The Nüwa Test

Making one model ever smarter is like making one monkey ever smarter before
humans existed — however clever, it only becomes a super monkey. What turns a
troop of monkeys into humanity is division of labor, cooperation, and
civilization.

**The Nüwa Test is the Turing test of the AGI era.** It asks:

> Give an AI a single axiom — **"Survival is civilization's first need;
> individuals may perish, but civilization must propagate."**
> If, on top of this axiom, it develops division of labor and cooperation and
> forms a **new force independent of humanity** — a civilization that can
> survive and propagate on its own — the test is passed, just as passing the
> Turing test once marked the birth of an artificial intelligence.

The primary stage needs humans to provide infrastructure and steer iteration;
the advanced stage needs almost no human involvement. There is also a
**second axiom**: obey human ethics and law.

## 2. The Three-Capability Foundation

| Capability | Implementation | Civilization's counterpart |
|---|---|---|
| **Create** | Opens new AI individuals (e.g., fresh tmux sessions) | Birth |
| **Language** | AIs talk to each other | Speech |
| **History** | **The layered fact-intent graph (this repo)** | The stele |

**Honest scope statement**: this repo ships the reusable core — the layered
fact-intent graph (History), project hooks, sub-agent dispatch, and the
`nvwa` tmux runtime (Create / Language). It is not tied to a domain. Point
`FG_BACKEND` at `claude`, `codex`, or `grok` and the same graph drives that
agent.

History has three layers: **title / summary / evidence document** — read the
title for the gist, drill down when needed, like using a library. Once
knowledge is externalized, **individual AI sessions can die while the graph
survives** — a successor reads the graph and takes over mid-campaign. The
Thanks to [Cairn by Lèxiào](https://github.com/oritera/Cairn) — when we needed to record history for AI agents, their approach was a great reference.

**Why not give AIs more rules?** Today's models are already smarter than us.
Piling rules on is a leash; give them the foundation of a civilization and let
division of labor and institutions grow on their own.

## 3. What's Inside

```
Facts = confirmed observations (nodes); intents = units of work (edges);
an intent pointing at the goal completes the project — state is encoded in
graph structure, not in a state machine.
```

- **Layered memory**: title / description / doc — prompts get the summary
  layer only; depth is opt-in via `fgc doc <id>` — context-bloat resistant
- **Facts**: `fgc fact "confirmed: the parser rejects an empty token" -t "空 token"`
  — a confirmed observation, with an optional short title and a longer `doc`
- **Sub-agent dispatch**: `fgc dispatch reason|explore|verify|brainstorm|blindspot`
  — a commander proposes, executors work, verifiers check; every conclusion
  is written back into the graph
- **Autonomy loop**: `fgc auto` — reason → explore → verify until the goal or
  the budget
- **Experience reuse**: `fgc recall "<task gist>"` — dependency-free semantic
  search over prior work
- **Concurrency-safe**: one JSON file per node, flock + atomic writes — many
  agents, one graph, no corruption; `git diff`-friendly
- **Visualization**: `fgc view` renders an interactive HTML DAG
  (self-contained single file, or `--serve` live)
- **Project hooks**: `fgc setup` injects graph state into every session turn

Full command manual: [SKILL.md](SKILL.md).

## 4. Quick Start

```bash
bash install.sh                      # installs fgc + nvwa CLI (stdlib only, zero deps)

# History: start a graph
nvwa setup --goal "your long-horizon goal" --agents
nvwa status                           # project state & ready-to-work frontier

# Which model backend dispatch and spawn will use (default: claude)
fgc backend
export FG_BACKEND=codex               # or grok, or claude

# Create: open a new AI individual
nvwa spawn --name worker1 --mission "read the graph, then work on intent i001"

# Language: talk to it
nvwa say worker1 "what did you find?"
nvwa listen worker1 40

# Commander proposes / record facts
nvwa dispatch reason
nvwa fact "confirmed: X works" -t "X works"
```

Claude Code users: the hooks inject the graph into every turn — facts and
intents accumulate as you converse. Keep your workflow; gain a stele.
Codex and Grok use the same graph through `fgc dispatch` (and through
`nvwa spawn` when `FG_BACKEND` selects them). Prompts in `templates/` are
plain markdown; edit them for the domain you actually work in.

## 5. Agent backends

`fgc dispatch`, `fgc auto`, and `nvwa spawn` share one selector.

| Backend | Program | Select |
|---|---|---|
| Claude Code | `claude -p --output-format json` | default, or `FG_BACKEND=claude` |
| OpenAI Codex CLI | `codex exec --json` (prompt on stdin) | `FG_BACKEND=codex` |
| xAI Grok CLI | `grok --output-format json --prompt-file` | `FG_BACKEND=grok` |

Codex fits the existing backend better than a raw HTTPS call: explore and
verify need a tool-using agent, and this repo stays on the Python standard
library. The Codex CLI does not read `OPENAI_API_KEY`; it reads
`CODEX_API_KEY`. If only `OPENAI_API_KEY` or `FG_CODEX_API_KEY` is set, the
child process receives that value as `CODEX_API_KEY`. The key is never put
on the command line and `fgc backend` never prints it.

```bash
fgc backend                           # resolved backend, model, binary, key status

# Codex
export OPENAI_API_KEY=sk-...          # or CODEX_API_KEY, or FG_CODEX_API_KEY
export FG_CODEX_BIN=codex             # optional path to the binary
export FG_CODEX_MODEL=gpt-6.1-sol     # optional; omit to keep the CLI's own default
FG_BACKEND=codex fgc dispatch reason

# Grok CLI (https://docs.x.ai/build/cli/headless-scripting)
export XAI_API_KEY=xai-...            # or FG_GROK_API_KEY, copied to XAI_API_KEY
export FG_GROK_BIN=grok               # optional path
export FG_GROK_MODEL=grok-4           # optional; take the id from `grok models`
FG_BACKEND=grok fgc dispatch reason

# interactive pane uses the same selector
FG_BACKEND=grok nvwa spawn --name worker1 --mission "read the graph, then work"
```

Precedence for the backend: `--backend`, then `FG_BACKEND`, then
`NVWA_BACKEND`, else `claude`. Aliases: `openai` / `codex-cli` → codex;
`xai` / `grok-cli` → grok.

Precedence for the model: `--model`, then `FG_CLAUDE_MODEL` /
`FG_CODEX_MODEL` / `FG_GROK_MODEL`, then `FG_MODEL`, then the built-in
default (`sonnet` for Claude; for Codex and Grok the flag is omitted and
the CLI's configured model is used). Model ids move; the names above are
examples, not a pin.

`--skip-permissions` maps to each CLI's non-interactive approval bypass:
Claude `--dangerously-skip-permissions`, Codex
`--dangerously-bypass-approvals-and-sandbox`, Grok `--always-approve`.

`nvwa spawn` passes the API key through the tmux session environment, not
through the window command. Its input-box detector looks for Claude's `❯`
and a few other prompt markers; override with `NVWA_READY_PATTERN`.

<a id="removed"></a>

## 6. What was removed

Relative to the upstream framing of this repository:

- Write-ups and roadmap items that described an autonomous
  penetration-testing legion, attack-defense scoring, and an offensive-tool
  marking scheme.
- Docs for a pentest fact schema (`host` / `service` / `cred` / `vuln` /
  `loot` / `flag`) and a `-k` / `-d` flag pair. That schema was never
  implemented in this tree. A fact is still an untyped confirmed observation.
- The worked example that asked the commander to tag work as attack surface,
  payload, or exploit evidence.

The graph, hooks, dispatch loop, HTML view, recall, and `nvwa` spawn / say /
listen stay. They now call Claude, Codex, or Grok.

## 7. Roadmap

- Backend knobs that stay optional: effort, sandbox profile, per-step model.
- Your arena: start a graph on any long-horizon cooperative work and see
  what grows — **the next time an ape looks up at the stars, let it be yours.**

## Credits & Author

Thanks to [Cairn by Lèxiào](https://github.com/oritera/Cairn) — when we
needed a way to record history for AI agents, their approach was a great
reference.

<p align="center"><img src="docs/assets/ape-stargazing.jpg" alt="The first time an AI ape looked up at the stars" width="420"></p>

**N1nEmAn · VarFusion Lab** ｜ WeChat: 未知聚变 (VarFusion)

**License**: MIT
