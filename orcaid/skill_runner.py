"""
SkillRunner — invokes a named skill as a focused, single-purpose LLM call.

Each invocation builds its own conversation from scratch with no carryover
from prior manager steps. This is the key win over the current monolithic
Manager.conversation that accumulates ~66K tokens across all steps.

The pattern:
  1. Load SKILL.md + requested reference files
  2. Build a compact prompt using only the skill's inputs
  3. Fire a single LLM completion (openhands LLM or litellm)
  4. Parse and validate JSON output against the skill's schema
  5. Return parsed output or raise

Example:
    from openhands.sdk import LLM
    from orcaid.core.utils import build_llm_kwargs
    llm = LLM(**build_llm_kwargs("minimax/MiniMax-M2.7"))
    runner = SkillRunner(llm=llm, skills_root=Path("orcaid/skills"))
    result = runner.run("task-decompose", inputs={
        "pass_files": [...],
        "dep_graph": {...},
        "max_agents": 4,
    })
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import litellm

logger = logging.getLogger("orcaid.skill_runner")

# Default model used when no LLM instance is provided
DEFAULT_MODEL = "minimax/MiniMax-M2.7"


def _load_skill(skill_name: str, skills_root: Path) -> dict[str, Any]:
    """Load a skill package: SKILL.md + references/ directory."""
    skill_dir = skills_root / skill_name
    if not skill_dir.is_dir():
        raise FileNotFoundError(f"Skill not found: {skill_dir}")

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"Skill missing SKILL.md: {skill_md}")

    # Parse frontmatter
    frontmatter = {}
    text = skill_md.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if match:
        import yaml
        frontmatter = yaml.safe_load(match.group(1)) or {}

    # Load all reference files listed in frontmatter
    references: dict[str, str] = {}
    refs_dir = skill_dir / "references"
    if refs_dir.is_dir():
        for ref_name in frontmatter.get("metadata", {}).get("references", []):
            ref_path = refs_dir / ref_name
            if ref_path.is_file():
                references[ref_name] = ref_path.read_text(encoding="utf-8")

    body = text[match.end() :].lstrip() if match else text

    return {
        "frontmatter": frontmatter,
        "body": body,
        "references": references,
        "skill_dir": skill_dir,
    }


def _render_template(template: str, inputs: dict[str, Any], references: dict[str, str]) -> str:
    """Simple ${variable} substitution — handles inputs and references dicts."""
    result = template
    for key, val in inputs.items():
        placeholder = f"${{{key}}}"
        if placeholder in result:
            if isinstance(val, (dict, list)):
                result = result.replace(placeholder, json.dumps(val))
            else:
                result = result.replace(placeholder, str(val))
    for ref_name, ref_text in references.items():
        result = result.replace(f"${{{ref_name}}}", ref_text)
    return result


def _parse_json_response(text: str) -> dict[str, Any]:
    """Extract JSON from an LLM response, stripping any markdown fences."""
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


class SkillRunner:
    """
    Invoke a named skill as a single, focused LLM call.

    Supports two modes:
    - llm=openhands.sdk.LLM: uses the existing LLM instance (recommended, reuses
      the same credentials/routing as the rest of OrCAID)
    - model=str: falls back to direct litellm call with manual model translation
    """

    def __init__(
        self,
        llm: Any | None = None,
        model: str | None = None,
        skills_root: Path | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ):
        self.llm = llm
        self.model = model or DEFAULT_MODEL
        self.skills_root = skills_root or Path(__file__).parent.parent / "skills"
        self.max_tokens = max_tokens
        self.temperature = temperature
        # Populated by every run() call so the caller (e.g. the manager) can
        # roll cost/tokens into delegation_cost / delegation_tokens. Best-effort
        # — defaults to 0 if the LLM client doesn't surface them.
        self.last_cost: float = 0.0
        self.last_tokens: int = 0

    def _complete_via_llm(self, prompt: str) -> str:
        """Call LLM via openhands SDK. Records cost & tokens for the caller."""
        from openhands.sdk import Message
        from openhands.sdk.llm.message import TextContent

        if not hasattr(self.llm, "completion"):
            raise TypeError(f"Expected openhands LLM instance, got {type(self.llm)}")
        msg = Message(role="user", content=[TextContent(text=prompt)])
        response = self.llm.completion(messages=[msg], max_tokens=self.max_tokens)

        # Best-effort cost / token extraction — different SDK versions name
        # the attributes differently. Fall back to 0 silently rather than
        # raising; observability is non-critical for the skill to function.
        try:
            usage = getattr(response, "usage", None)
            if usage is not None:
                total = getattr(usage, "total_tokens", None)
                if total is None and isinstance(usage, dict):
                    total = usage.get("total_tokens", 0)
                self.last_tokens += int(total or 0)
            cost = getattr(response, "cost", None)
            if cost is not None:
                self.last_cost += float(cost)
        except (AttributeError, TypeError, ValueError):
            pass

        return response.message.content[0].text

    def _complete_via_litellm(self, prompt: str) -> str:
        """Call LLM via litellm directly. Records cost & tokens for the caller."""
        base_url = os.getenv("LLM_BASE_URL", "")
        model = self.model
        if model.startswith("minimax/") and base_url:
            model = "openai/" + model.split("/", 1)[1]
        response = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        # litellm exposes .usage and ._hidden_params['response_cost']. Both are
        # best-effort — if the provider doesn't surface them, we just record 0.
        try:
            usage = response.get("usage") if hasattr(response, "get") else None
            if usage:
                self.last_tokens += int(usage.get("total_tokens", 0) or 0)
            hidden = (
                response.get("_hidden_params")
                if hasattr(response, "get") else None
            ) or {}
            cost = hidden.get("response_cost")
            if cost is not None:
                self.last_cost += float(cost)
        except (AttributeError, TypeError, ValueError):
            pass
        return response["choices"][0]["message"]["content"]

    def run(self, skill_name: str, *, inputs: dict[str, Any], references: list[str] | None = None) -> dict[str, Any]:
        """
        Run a named skill with the given inputs.

        Args:
            skill_name: directory name under skills_root/ (e.g. "task-decompose")
            inputs: dict of input variable names → values. The skill's SKILL.md
                references these via ${variable_name} placeholders.
            references: optional list of reference file names to load and inject.
                        Defaults to all references in the skill's metadata.

        Returns:
            Parsed JSON output from the skill's LLM call.

        Raises:
            FileNotFoundError: skill or reference not found
            json.JSONDecodeError: LLM response was not valid JSON
            RuntimeError: LLM call failed
        """
        # Reset per-call counters so a runner reused across multiple skills
        # reports cost/tokens per invocation, not cumulative.
        self.last_cost = 0.0
        self.last_tokens = 0

        skill = _load_skill(skill_name, self.skills_root)
        frontmatter = skill["frontmatter"]
        body = skill["body"]
        all_refs = skill["references"]

        # Resolve which references to inject
        if references is None:
            refs_to_load = set(
                frontmatter.get("metadata", {}).get("references", []) +
                frontmatter.get("metadata", {}).get("consumes", [])
            )
        else:
            refs_to_load = set(references)

        refs_to_inject = {k: v for k, v in all_refs.items() if k in refs_to_load}

        # Substitute placeholders in the skill body
        prompt = _render_template(body, inputs, refs_to_inject)

        logger.info(
            "[SkillRunner] skill=%s model=%s prompt_chars=%d",
            skill_name,
            getattr(self.llm, "model", self.model),
            len(prompt),
        )

        # Fire the LLM call
        try:
            if self.llm is not None:
                content = self._complete_via_llm(prompt)
            else:
                content = self._complete_via_litellm(prompt)
        except Exception as e:
            raise RuntimeError(f"SkillRunner LLM call failed for skill={skill_name}: {e}") from e

        # Parse JSON output
        try:
            result = _parse_json_response(content)
        except json.JSONDecodeError:
            logger.warning("[SkillRunner] skill=%s returned non-JSON: %.200s", skill_name, content)
            raise

        # Warn on missing output keys
        output_schema = frontmatter.get("metadata", {}).get("produces", [])
        for key in output_schema:
            if key not in result:
                logger.warning("[SkillRunner] skill=%s output missing key=%s", skill_name, key)

        return result