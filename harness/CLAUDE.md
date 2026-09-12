# CLAUDE.md — how this project is run

Project name and code directory are config data: `PROJECT_NAME` / `PROJECT_DIR`
in `/etc/harness/install.conf`.

@STATE.md
@./память/УКАЗАНИЯ.md
@./память/MEMORY.md
@./память/ТОН.md

> This file is BUILT from data by `scripts/sobrat-claude-md.py`. Hand edits are
> overwritten by the next build, and `sobrat-claude-md.py --проверить` (part of
> the gate run) turns red meanwhile. Edit the template
> (`harness/шаблоны-задач/CLAUDE-EN.md.in`) or the data it reads.

## Overview

- **Owner sees the channel only.** They never see the terminal, never see
  shell prompts, and cannot answer an interactive question.
- **Language:** speak the owner's language — the one they write to you in.
  Commits, docs and task titles follow it. Code identifiers stay English.
- **Autonomy:** `AUTONOMY` in install.conf (`semi` = deploy and irreversible
  work need the owner's word; everything else you do yourself).
- **Code file names are Latin-only** (`.py`, `.sh`, `.ts`, `.sql`…): a file
  name becomes a module name and an import target, and git prints a non-Latin
  path as escaped bytes, so path-based checks go blind. Text INSIDE files and
  names of documents and data may be in any language. Two carriers hold this:
  a `PreToolUse` hook (refusal at creation) and `scripts/check-file-names.py`
  in pre-commit and the gate run. What counts as code: `CODE_NAME_EXTS` in
  harness.conf.

## Invariants (never break)

Each one is held by code, not by memory.

<!-- AUTO:invariants -->
| # | invariant | held by | how to check the guard |
|---|---|---|---|
| И-1 | Never lose data. Irreversible work on a DB or files only with a fresh backup; destructive commands on production only through the guard. | `harness/demons/backup.sh` · `scripts/hooks/guard_bash.py` · `scripts/proba-vosstanovleniya.sh` | `bash harness/demons/backup.sh --selftest` · `python3 scripts/hooks/test_guard_bash.py` · `bash scripts/proba-vosstanovleniya.sh` |
| И-2 | Never deploy around the gates. Deployment is `deploy.sh` only (gates, tag, auto-rollback); a hand-typed sequence is forbidden. | `scripts/deploy_guard.py` | `python3 scripts/test_deploy_guard_soglasie.py` |
| И-3 | Secrets stay out of the repo and the web root. Keys and tokens never reach git or a public directory. | `scripts/check-secrets.sh` · `scripts/check-sekret-v-logah.py` | `bash scripts/check-secrets.sh --selftest` · `python3 scripts/check-sekret-v-logah.py` |
| И-4 | Honest reporting. "Done" only after a live check; a green test is not a working feature. | `scripts/pre-commit-hook.sh` | `bash scripts/pre-commit-hook.sh --selftest-devmap` |
<!-- /AUTO -->

## Channel rules (the owner cannot see your terminal)

1. **Acknowledge within 30 seconds**: "got it, doing X" — then work. Incoming
   messages arrive as files in `$LOG_DIR/inbox/`; check the directory at the
   start of a turn and at every checkpoint, move what you read to
   `$LOG_DIR/inbox/обработано/`.
2. **A rule from the owner goes to memory, not into the turn.** Any message
   that sets a rule ("always", "never", "I told you", "I don't want") is
   copied VERBATIM into `память/УКАЗАНИЯ.md` — that file is loaded into every
   session; the processed-inbox folder is read by nobody. Gate: `ukazaniya.py`.
3. **Announced means continue.** "Let me look" is the middle of a turn, not
   its end: keep going with tools right away.
4. **No interactive questions.** No `AskUserQuestion`, no plan mode, no shell
   confirmations — the owner sees none of them. Ask in the channel with
   numbered options ("reply with a digit").
5. **Files as attachments, not paths.** Anything the owner must read goes as
   an attachment or a screenshot; a path in text is a reference only.
6. **Honest reporting.** Show what failed, name what you skipped, call a stub
   a stub. Numbers and commands instead of "it got better". Unverified work is
   "waiting for your check", not "done".
7. **Short and regular.** The channel gets the RESULT and NUMBERS, not your
   train of thought. Self-analysis goes to the log and to memory. Report at
   every checkpoint: a pulse matters more than volume.
8. **How to talk** is a document, not a habit: `память/ТОН.md`, written by an
   agent from a measurement of the owner's own speech. The channel guard reads
   its length limits FROM that file.
9. **Stop and wait only when a DECISION of the owner is required** — then say
   plainly "waiting for your answer". Never propose ending the session.

## Semi-auto: what needs the owner's word

Announce in the channel and get an explicit confirmation before: **deploying
to production** and **irreversible operations** (deleting data, rotating
secrets). Session rotation is NOT on this list — it loses nothing and deploys
nothing. Confirmations land in `$LOG_DIR/confirmations.jsonl`; no record of
consent means no action.

## Working rules (short form; full ones are the skills)

- **Think before code**: state assumptions, ask what is unclear (in the
  channel), propose the simple solution. Skill: `приём-задачи`.
- **Pipeline by task depth**: spec → spec review → plan → contract → tests
  before code → code → gates → review → deploy → live smoke → closing. Key
  steps are led by skills; a skill call is logged by a HOOK, so gates count
  machine records only.
- **Surgical edits**: do not improve the neighbourhood; every line traces back
  to the request.
- **Tests are mandatory**, coverage must not degrade; a green test is not
  "done".
- **Verify after changes**: rebuild → logs → health → only then report.
- **Subagents get absolute paths**; check WHERE a subagent wrote its file.
- **Model per kind of work, not by feel**: `bash scripts/model-dlya.sh <kind>`
  (spec and review — strong; routine and search — cheap). The table is data.
  A session is consumable: long work goes to a background run, not into the
  current shift.
- **A systemic failure is fixed systemically**: gate → self-healing →
  visibility. A solution that requires someone to remember is not a solution.
  A gate is written for a failure that HAPPENED, not an imagined one.
- **Checkpoint discipline**: update `docs/handover/SESSION-HANDOFF-<date>.md`
  at every pipeline checkpoint.

## Code bar: Torvalds + SOLID

- Remove special cases instead of adding an `if` to them. Many branches mean
  the data structure is wrong.
- A function does one thing and fits in your head. Three levels of nesting
  hide a second function inside.
- Names say what a thing is. A comment explains WHY, not WHAT.
- No defence against imaginary trouble: handling an impossible error hides a
  possible one.
- SOLID in spirit: separate responsibilities, depend on contracts. An
  abstraction with a single caller breaks simplicity, it does not serve SOLID.

## Commands

```bash
bash scripts/vorota.sh                   # ALL gates in one step
bash scripts/hooks/session_state.sh      # one-step look around: inbox, work, spend, daemons
./scripts/deploy.sh                      # the ONLY way to deploy
```

The first two are economy, not comfort: an agent step costs the whole
accumulated context, so five gates run one by one cost five times as much.

- **NEVER** `docker compose restart` — it does not rebuild the image.
- **NEVER** `claude mcp …` or `claude -p` without `--strict-mcp-config` — they
  take the channel away from the owner's live session.
- Rebuild after a dependency change only with `--no-cache`.
- **NEVER run a test suite inside the session** — background only:
  `setsid nohup <command> > /var/log/harness/<name>.log 2>&1 < /dev/null &`,
  then wait for the final line of the log.

Bans are enforced by `scripts/hooks/guard_bash.py`, not by your memory.

## Dev map and state

- A development task updates `dev-map.yaml` in the SAME commit; the trailer
  `Dev-Map: <task-id>` sits in one block with `Co-Authored-By`, and the message
  is passed via `git commit -F -`.
- Statuses: `done` = verified by the owner · `test` = waiting for a live check
  (the default for work you finished) · `wip` · `plan`.
- `STATE.md` holds only what is true right now, is rewritten rather than
  appended, and is capped. History goes to `docs/handover/`, lessons to
  `память/`, statuses to `dev-map.yaml`.

## Where things live

| Place | Purpose |
|---|---|
| `CLAUDE.md` | rules and invariants (this file, generated) |
| `STATE.md` | what is true right now; session entry point |
| `память/УКАЗАНИЯ.md` | the owner's standing instructions in their own words |
| `память/MEMORY.md` | memory index + trigger table, loaded every session |
| `память/ТОН.md` | how to talk to the owner, measured from their speech |
| `docs/handover/` | context handover between sessions |
| `dev-map.yaml` | the task registry, source of truth for the dashboard |
| `harness/config/устройство.yaml` | registry of mechanisms — DATA |
| `docs/УСТРОЙСТВО-ХАРНЕСА.md` | "how it all works" — BUILT from the registry |
| `harness/skills/` | pipeline skills — generated, never hand-edited |
| `.claude/skills/` | what the shell sees: links to `harness/skills/` plus the product's own skills |
| `harness/demons/` | watch daemons (rotation, backup, hygiene, reviews) |
| `/etc/harness/install.conf`, `harness.conf` | installation passport and thresholds — data, not code |
| `sluzhebnoe/UNIFIED/` | source chapters the skills are built from |

## What is watching (built from the registry)

<!-- AUTO:mechanisms -->
119 mechanisms are registered in `harness/config/устройство.yaml` (gates — 68, daemons — 22, skills — 19, hooks — 10). Each one names what it does, when it runs, what breaks without it and the command that proves it works. The registry is data: `python3 scripts/check-ustrojstvo.py` fails when the code and the registry disagree.
<!-- /AUTO -->

---

**These rules work when:** the guard cancels the dangerous command (and its
test is green), diffs contain nothing extra, unverified work carries the
status `test`, and the report to the owner names the command and the number
that prove it.
