# Conventions

**Analysis Date:** 2026-05-18

This document codifies the coding standards, styling patterns, file practices, dynamic module loading procedures, type hints, and version control guidelines required in the OrCAID repository.

---

## 1. Code Style & Syntax

OrCAID adheres to standard PEP8 layout configurations enforced programmatically via Ruff.

- **Maximum Line Length:** 120 characters.
- **Indentation:** 4 spaces (strictly no tabs).
- **Naming Styles:**
  - **Modules & Files:** `snake_case` (e.g., `orcaid_verification_bridge.py`, `manager_review.py`).
  - **Classes & Types:** `PascalCase` (e.g., `SubAgentRunner`, `AssignmentMixin`).
  - **Functions & Methods:** `snake_case` (e.g., `_verify_and_return`, `run_subagents_parallel`).
  - **Variables & Fields:** `snake_case`.
  - **Constants:** `UPPER_SNAKE_CASE` (e.g., `ORCAID_TO_HERMES_PROFILE`).

---

## 2. Modern Type Hinting Standards

To leverage full static analysis benefits in Python 3.12, legacy `typing` generic representations are deprecated.

- **Generics:** Use native collection types directly.
  - **Incorrect:** `from typing import List, Dict, Set, Tuple`
  - **Correct:** Use `list`, `dict`, `set`, `tuple` directly.
- **Unions & Optionals:** Use the native union operator pipe (`|`) instead of `typing.Union` or `typing.Optional`.
  - **Incorrect:** `Optional[str]`, `Union[int, float]`
  - **Correct:** `str | None`, `int | float`
- **Strict Typing:** All functions, methods, and class fields must have complete type signatures, including arguments and return statements.

---

## 3. Inline Documentation & Docstrings

All files, modules, classes, and public methods must contain comprehensive inline docstrings.

- **Format:** Google-Style docstrings.
- **Layout:**
  - One-line high-level description.
  - Bulleted argument details including type and meaning.
  - Explicit return descriptions.
  - List of raised exceptions.
- **Example:**
  ```python
  def calculate_accumulated_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
      """Calculate total run cost using prompt and completion token counts.

      Args:
          prompt_tokens: The absolute number of input tokens.
          completion_tokens: The absolute number of output tokens.

      Returns:
          A float representing the total dollar cost of the API call.

      Raises:
          ValueError: If token counts are negative.
          ZeroDivisionError: If the pricing denominator is zero.
      """
  ```

---

## 4. Hardened File Operations

To prevent platform-dependent encoding bugs (e.g. Debian vs. Windows defaults), all file reading and writing must explicitly define an encoding.

- **Rule:** Every call to `open()` must define `encoding="utf-8"`.
  - **Incorrect:** `with open("config.yaml", "r") as f:`
  - **Correct:** `with open("config.yaml", "r", encoding="utf-8") as f:`
- **File Modes:** Always use context managers (`with` statements) for predictable socket and handle closing.

---

## 5. Strict Error & Exception Handling

OrCAID mandates resilient error wrapping to prevent process crashes during long-running async orchestrations.

- **No Bare Exceptions:** Never write bare `except:` statements. Always catch targeted exception types.
- **Fallback Configurations:** Dynamically loaded components must be guarded by robust import checks with fallback states.
- **Trace Logs:** Print or log clear traceback reports whenever critical subprocess failures occur.
- **No Quiet Swallowing:** If an exception is caught, it must be handled (logged, escalated, or translated into an agent correction context). Do not silent-fail.

---

## 6. Git & Version Control Conventions

OrCAID repositories enforce clean, atomic git histories.

- **Commit Message Format:** Adhere strictly to the **Conventional Commits** standard:
  - `feat: ...` for new functional features.
  - `fix: ...` for bug fixes.
  - `refactor: ...` for internal structural restructuring.
  - `docs: ...` for documentation additions or changes.
  - `test: ...` for writing unit or integration tests.
  - `chore: ...` for updating lockfiles, packaging, or dev tools.
- **Commit Granularity:** Commits should be atomic (focusing on one specific problem or feature segment). Do not group unrelated changes into a single bulk commit.
