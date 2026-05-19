# Delegation Rubric

Rules for assigning pass-stub implementation tasks to engineers.

## Core Principles

1. **Balance complexity evenly** — split work so no engineer is overloaded
2. **Keep dependent files together** — circularly dependent files go to the same engineer
3. **Prefer file-level delegation** — split at file level first; only go function-level when one file dominates
4. **Engineers share `/workspace/`** — no file isolation; specify non-overlapping assignments explicitly
5. **Order by dependency** — assign files with no dependencies first

## Output Schema

Output a single JSON object:

```json
{
  "delegation_plan": {
    "first_round": {
      "num_agents": 1,
      "reasoning": "...",
      "tasks": [
        {
          "engineer_id": "engineer_1",
          "task_id": "task-unique-id",
          "file_path": "path/to/file.py",
          "functions_to_implement": ["func1", "func2"],
          "complexity": "simple|medium|complex",
          "instruction": "Explain repo structure, dependencies, and what to implement."
        }
      ]
    },
    "remaining_tasks": [
      {
        "task_id": "task-unique-id",
        "file_path": "path/to/file.py",
        "functions_to_implement": ["func1", "func2"],
        "depends_on": ["list of file_paths"],
        "complexity": "simple|medium|complex"
      }
    ]
  }
}
```

## Assignment Rules

- `engineer_id`: "engineer_1", "engineer_2", etc.
- `task_id`: unique per task (e.g. "task_1", "task_2" or derived from file)
- `file_path`: relative path from repo root
- `functions_to_implement`: list of function names with `pass` bodies in that file
- `complexity`: "simple" (1-2 funcs), "medium" (3-5), "complex" (6+)
- `depends_on`: empty list if no dependencies, otherwise list of file paths this file imports from
- `instruction`: must include (a) repo structure summary, (b) purpose of the file, (c) expected behavior of functions, (d) any stub dependencies the engineer needs to know about

## Dependency Rules

- A file with no imports from other pass-stub files gets highest priority
- If two files are circularly dependent (each imports the other), assign them to the same engineer
- Engineers work from simple to complex tasks
- Never assign engineers to implement functions that don't have `pass` bodies in the scanned files