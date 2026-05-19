# Skill-Based Manager Refactor — Design Doc

**Status:** proposal — pre-implementation
**Trigger:** Minimax-M2.7 commit0 run on 2026-05-18 produced an empty delegation plan
because the monolithic delegation prompt overflowed the model's effective context (66K
input tokens, no JSON in response). Diagnosed in conversation; this doc proposes a
structural fix rather than a model-swap workaround.

---

## 1 — Why the current shape is broken

`orcaid/core/manager.py` (via its four mixins) drives the workflow as **two monolithic
LLM calls** plus per-engineer work:

| Step | Where | Cost on Minimax run | What's wrong |
|---|---|---|---|
| `scan_and_analyze()` | `manager.py` + `prompts/commit0.yaml::scan_analysis` | 32K input tokens | LLM is asked to do *both* deterministic work (find files with `pass` statements, build dependency graph) *and* judgment work (priority reasoning). The deterministic half can be a Python script. |
| `delegate_tasks()` | `manager.py` + `prompts/commit0.yaml::user_instruction` | 66K input tokens — **failed to emit JSON** | Carries the entire prior conversation (the scan output + the full task prompt + the delegation rubric + the JSON schema) into one call. Minimax-M2.7 dropped into chat mode and asked "what would you like me to help with?" instead of emitting the delegation plan. |
| `onboard_subagents()` | `manager.py` | 0 (skipped — no tasks) | Would have run once per engineer, also carrying the accumulated context. |
| `final_review_all()` | `manager_review.py` | 0 (skipped — no engineers ran) | Another monolithic LLM call. |

The skills design pattern from
[`/home/ty/Documents/LLM-WIKI/raw/What the docs don't tell you about Claude Code skills.md`](file:///home/ty/Documents/LLM-WIKI/raw/What%20the%20docs%20don%27t%20tell%20you%20about%20Claude%20Code%20skills.md)
identifies this exact failure mode as "everything in the SKILL.md body, nothing in
`references/`": the model carries lookup tables and large artifacts in its working
context when they could be loaded on demand into focused sub-steps.

The fix is the same at the agent layer as at the skill layer: **progressive disclosure**.

---

## 2 — Target architecture

Replace the two monolithic LLM calls with a sequence of small, focused steps. Each step
is either a deterministic Python script (no LLM) or an LLM call with a narrow,
purpose-built prompt that sees only the artifact it needs.

```
Manager (orchestrator — small loop, no LLM judgment of its own)
    │
    ├── 1. repo-scan
    │     ├── scripts/find_pass_stubs.py   (deterministic, no LLM)
    │     └── (optional) LLM micro-call for "any non-obvious stubs?" if user opts in
    │     → outputs/scan/pass_files.json
    │
    ├── 2. dependency-graph
    │     └── scripts/build_dependency_graph.py  (deterministic, no LLM)
    │     → outputs/scan/dep_graph.json
    │
    ├── 3. task-decompose                  (LLM call — small, focused)
    │     prompt input: pass_files.json + dep_graph.json + delegation_rubric.md
    │     prompt output: delegation.json
    │
    ├── 4. engineer-onboard                (LLM call — one per engineer)
    │     prompt input: one task_node + relevant file slices
    │     prompt output: engineer prompt string
    │
    │   (engineers run in worktrees — unchanged from today)
    │
    ├── 5. review-and-verify               (per engineer, already exists)
    │     calls _verify_and_return() → bridge.py → Phase A/B fires here
    │
    └── 6. final-synthesis                 (LLM call — small, focused)
          prompt input: per-engineer SubAgentResult summaries (not full diffs)
          prompt output: final report
```

### Context budget per LLM call (estimated)

| Step | Approx. input tokens | What it sees |
|---|---|---|
| `task-decompose` | 5–8K | pass_files.json (~1K) + dep_graph.json (~1K) + delegation_rubric.md (~1K) + commit0 task framing (~2K) + JSON schema (~1K) |
| `engineer-onboard` | 2–4K | one task_node + the target file's stub content (sliced, not whole repo) |
| `final-synthesis` | 3–5K | per-engineer summary table only (commit hashes, files modified, test pass count) — *not* full diffs |

vs. current 66K-token monolith → ~75% reduction in worst-case context, and the
deterministic half goes from 32K LLM tokens to 0 LLM tokens.

---

## 3 — File layout

### New (skills + scripts)

```
orcaid/
├── skills/                                 # NEW — skill packages
│   ├── repo-scan/
│   │   ├── SKILL.md
│   │   └── scripts/
│   │       └── find_pass_stubs.py
│   ├── dependency-graph/
│   │   ├── SKILL.md
│   │   └── scripts/
│   │       └── build_dependency_graph.py
│   ├── task-decompose/
│   │   ├── SKILL.md
│   │   └── references/
│   │       ├── delegation_rubric.md
│   │       └── engineer_profile_map.md
│   ├── engineer-onboard/
│   │   └── SKILL.md
│   └── final-synthesis/
│       └── SKILL.md
└── skill_runner.py                         # NEW — invokes a skill by name
```

### Modified

```
orcaid/
├── core/
│   ├── manager.py                          # MODIFIED — shrinks to a thin orchestrator
│   ├── manager_exploration.py              # MODIFIED — calls repo-scan skill
│   ├── manager_assignment.py               # MODIFIED — calls task-decompose skill
│   └── manager_review.py                   # MOSTLY UNCHANGED — already uses bridge
├── tasks/
│   ├── commit0.py                          # MODIFIED — uses skill chain instead of inline prompts
│   ├── paperbench.py                       # MODIFIED — same
│   └── self_improve.py                     # MODIFIED — same
└── prompts/
    ├── commit0.yaml                        # DEPRECATED but kept for backward-compat
    └── paperbench.yaml                     # DEPRECATED but kept for backward-compat
```

### Unchanged (Phase A/B + verification bridge)

```
orcaid/
├── bridge.py                               # unchanged — Phase A/B keeps working
└── bond_classifier.py                      # unchanged
```

This is the important compatibility point: **the skill refactor and Phase A/B compose
cleanly**. Skills replace the manager-side prompts; Phase A/B operates on the engineer
result objects, which are unchanged.

---

## 4 — Skill anatomy (one concrete example)

### `orcaid/skills/task-decompose/SKILL.md`

```markdown
---
name: task-decompose
description: |
  Convert a list of files-with-pass-statements and a dependency graph into a
  JSON delegation plan that assigns engineers to functions. Use when the user
  has run repo-scan and dependency-graph and needs to fan out work to N
  parallel engineers. Do NOT use for repo exploration or stub discovery —
  those have dedicated skills.
allowed-tools: Read
metadata:
  version: "1.0"
  consumes:
    - outputs/scan/pass_files.json
    - outputs/scan/dep_graph.json
  produces:
    - outputs/delegations/delegation.json
---

## Inputs
- `${PASS_FILES_PATH}`: JSON file with `[{file, functions: [...]}]`
- `${DEP_GRAPH_PATH}`: JSON file with `{file: [dependencies]}`
- `${N_ENGINEERS}`: integer, number of available engineers
- `${ENGINEER_PROFILES}`: list of engineer profile names

## Steps
1. Read the two input JSON files.
2. Read `references/delegation_rubric.md` for the assignment rules.
3. Compute a topological order so engineers don't get blocked on each other.
4. Assign tasks to engineers, balancing weight across the N slots.
5. Emit a single JSON object with the schema below — nothing else.

## Output Schema
{ ... strict JSON schema with no prose around it ... }

## Constraints
- The response MUST be a single JSON object and nothing else.
- If you cannot produce a valid plan, emit `{"delegation_plan": {}, "error": "..."}`.
- Do not include backticks, markdown headers, or commentary in the response.
```

### `orcaid/skills/task-decompose/references/delegation_rubric.md`

The current ~40-line delegation rubric from `prompts/commit0.yaml::user_instruction`,
extracted as a stand-alone document. Loaded by the skill only when invoked — not
carried through the whole manager conversation.

### `orcaid/skill_runner.py`

```python
class SkillRunner:
    """Invokes a skill by name with a tight, single-purpose LLM call.

    Each invocation builds its own conversation from scratch — no carryover
    from prior steps. This is the key win over the current monolithic
    Manager.conversation that accumulates context.
    """

    def __init__(self, model: str, skills_root: Path = Path("orcaid/skills")):
        self.model = model
        self.skills_root = skills_root

    def run(self, skill_name: str, *, inputs: dict, references: list[str] = None) -> dict:
        """Load SKILL.md + requested references, build a fresh LLM call,
        return parsed JSON output. Validates against the skill's schema."""
        ...
```

---

## 5 — Migration sequence (validate after each step)

### Step A — Extract the deterministic bits (smallest change, biggest immediate win)

1. Create `orcaid/skills/repo-scan/scripts/find_pass_stubs.py`. Pure Python: walk repo,
   grep for `pass\s*$` inside function bodies via `ast`, return JSON.
2. Create `orcaid/skills/dependency-graph/scripts/build_dependency_graph.py`. Pure
   Python: `ast.parse` each pass-file, walk imports, return adjacency dict.
3. In `manager_exploration.py`, call these scripts *before* the existing scan_analysis
   LLM call. Pass their output into the LLM prompt as compact JSON instead of asking
   the LLM to compute them.
4. Validation: re-run the same Minimax sqlfluff command. Expect scan_analysis input
   tokens to drop from 32K to ~6K. Check `outputs/.../scan_analysis.json` for correct
   structure.

**Stop here if Minimax now produces a delegation plan.** Steps B–D are the structural
fix; they're worth doing for cost, testability, and Phase A/B effectiveness even if
Step A alone unblocks the run.

### Step B — Build `SkillRunner` and the `task-decompose` skill

1. Write `orcaid/skill_runner.py` (~120 lines).
2. Write `orcaid/skills/task-decompose/SKILL.md` + `references/delegation_rubric.md`.
3. Add a `--use_skills` flag to the CLI that flips between the old monolithic path and
   the new skill-based path.
4. Validation: A/B run with the flag. New path's `delegation.json` should match the old
   path's structure on the same input.

### Step C — Migrate the remaining LLM calls to skills

1. `engineer-onboard` skill (per-engineer prompt construction)
2. `final-synthesis` skill (final review)
3. Update `manager_assignment.py` and `manager_review.py` to dispatch through
   `SkillRunner` instead of `self.conversation.send_message`.

### Step D — Deprecate the prompts/ YAML files

1. Keep `prompts/commit0.yaml` and `prompts/paperbench.yaml` for one release cycle,
   marked deprecated.
2. Document the skills/ layout in `ARCHITECTURE.md`.
3. Remove the YAML files in the following release.

---

## 6 — How this composes with Phase A/B

Phase A (MOP-retry policy) and Phase B (three-bond drift classifier) operate
**downstream** of the manager. They fire in `_verify_and_return()` after a subagent
produces a `SubAgentResult`. Neither cares how the engineer prompt was built.

This refactor doesn't touch `bridge.py` or `bond_classifier.py`. It just makes the
manager actually produce engineers that *do* something, which is what unblocks Phase
A/B from having any data to work with.

There's one small benefit: when the new `final-synthesis` skill runs, it sees the
`SubAgentResult.missing_bond` field that Phase B now populates. The final-synthesis
prompt can list "dominant deficit pattern: self_reflection" alongside the merge stats,
which closes the loop — the manager's final report tells you not just *what* happened
but *what kind of failure* happened.

---

## 7 — Tradeoffs and risks

### Pros
- **Unblocks short-context models** — Minimax-M2.7 now sees 5K tokens per call instead
  of 66K. Also benefits Haiku, Mistral, any local model.
- **30-40K free tokens** — the deterministic extraction step alone eliminates LLM cost
  that was paying for grep + AST walk.
- **Composability** — `task-decompose` works the same way for commit0 and paperbench;
  the current code duplicates the prompt with minor variations.
- **Testability** — `find_pass_stubs.py` and `build_dependency_graph.py` get real unit
  tests against fixture repos. Right now those code paths are LLM-only.
- **Aligns with skills design pattern** — the doc Ty linked argues this is the
  *production-grade* way to structure agent prompts.

### Cons
- **Real refactor effort** — Step A is a few hours, Steps B–D realistically 1–2 days
  with tests.
- **Two paths to maintain during migration** — the `--use_skills` flag adds branching
  in `cli.py` until Step D lands.
- **Skill-runner is new infrastructure** — needs its own tests, error paths, schema
  validation. ~150 lines net.
- **Behavioral differences possible** — the monolithic manager might be using
  cross-step context in subtle ways (e.g., the analysis call influencing the
  delegation call's phrasing). The skill-based version starts each call fresh; we
  should A/B-test that the delegation quality doesn't regress on long-context models
  even as it improves on short-context ones.

### Things to watch
- The `task-decompose` LLM call must reliably emit valid JSON. The current
  monolithic call fails this in exactly the scenario that motivated the refactor. The
  skill's `SKILL.md` instructions must be aggressive about JSON-only output (see the
  xlsx skill in the Anthropic skills repo for the right level of explicitness).
- The skill format should be compatible with Claude Code's `.claude/skills/` discovery
  so the same skills can be invoked by a human running Claude Code on the OrCAID
  codebase (Hermes-style use case), not just by the OrCAID manager. Confirm the SKILL.md
  schema matches before committing to the file layout.

---

## 8 — Open questions to resolve before Step A

1. **SkillRunner LLM client** — reuse `litellm.completion()` directly, or wrap it
   through openhands-sdk's conversation primitives? The latter gets us event logging
   for free but ties us to the SDK's max_tokens / streaming behavior.
2. **Backward compatibility** — do we want existing `prompts/commit0.yaml`-based runs
   to keep working indefinitely, or commit to the migration?
3. **Schema validation** — do we want strict pydantic schemas on skill outputs (catches
   malformed JSON immediately) or stay with the existing best-effort
   `parse_json_from_response`?
4. **Should the manager itself live as `.claude/skills/orcaid-manager/`?** That makes
   the manager itself invokable by Claude Code as a skill — which is interesting for
   Hermes-style top-level orchestration but probably out of scope for this refactor.

---

## 9 — Next step if this design is approved

Begin **Step A only**. Land it, re-run the same Minimax sqlfluff command, measure the
new token usage and whether the delegation step now produces JSON. Decide on B–D
based on that measurement.

Estimated effort for Step A: 3 hours including tests.
