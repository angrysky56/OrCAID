"""
python -m judge.judge_runner
"""

import asyncio
import json
import os
from pathlib import Path

import fire
from litellm import cost_per_token

try:
    from paperbench.judge.create_judge import create_judge, handle_judge_kwargs
    from paperbench.judge.simple import ParsedJudgeResponseFloat, ParsedJudgeResponseInt
    from paperbench.judge.token_usage import get_total_token_usage
    from paperbench.paper_registry import paper_registry
    from paperbench.rubric.tasks import TaskNode
    from preparedness_turn_completer.oai_completions_turn_completer import (
        OpenAICompletionsTurnCompleter,
    )

    HAS_PAPERBENCH = True
except ImportError:
    create_judge = None
    handle_judge_kwargs = None
    ParsedJudgeResponseFloat = None
    ParsedJudgeResponseInt = None
    get_total_token_usage = None
    paper_registry = None
    TaskNode = None
    OpenAICompletionsTurnCompleter = None
    HAS_PAPERBENCH = False

DEFAULT_DATA_DIR = str(Path(__file__).resolve().parents[1] / "data" / "paperbench")


def configure_litellm_for_model(model_name: str):
    """
    Dynamically maps LITELLM_API_KEY/LLM_API_KEY and LITELLM_BASE_URL/LLM_BASE_URL
    to the correct provider-specific environment variables that LiteLLM expects.
    """
    api_key = os.environ.get("LITELLM_API_KEY") or os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LITELLM_BASE_URL") or os.environ.get("LLM_BASE_URL")

    if not api_key:
        return

    # Detect provider prefix
    provider = "openai"
    if "/" in model_name:
        provider = model_name.split("/")[0].lower()

    # Map to provider-specific env variables
    if provider == "minimax":
        os.environ["MINIMAX_API_KEY"] = api_key
        if base_url:
            os.environ["MINIMAX_API_BASE"] = base_url
    elif provider == "anthropic":
        os.environ["ANTHROPIC_API_KEY"] = api_key
        if base_url:
            os.environ["ANTHROPIC_BASE_URL"] = base_url
    elif provider == "gemini":
        os.environ["GEMINI_API_KEY"] = api_key
        if base_url:
            os.environ["GEMINI_API_BASE"] = base_url
    elif provider == "groq":
        os.environ["GROQ_API_KEY"] = api_key
        if base_url:
            os.environ["GROQ_API_BASE"] = base_url
    elif provider == "deepseek":
        os.environ["DEEPSEEK_API_KEY"] = api_key
        if base_url:
            os.environ["DEEPSEEK_API_BASE"] = base_url
    elif provider == "openrouter":
        os.environ["OPENROUTER_API_KEY"] = api_key
        if base_url:
            os.environ["OPENROUTER_API_BASE"] = base_url
    else:
        # Fallback/default to OpenAI
        os.environ["OPENAI_API_KEY"] = api_key
        if base_url:
            os.environ["OPENAI_BASE_URL"] = base_url

    # Always set OPENAI_API_KEY and OPENAI_BASE_URL if base_url is set,
    # as the paperbench judge's turn completer (OpenAICompletionsTurnCompleter)
    # uses the standard 'openai' package under the hood and requires these
    # to route to custom/OpenAI-compatible endpoints (like MiniMax).
    if base_url:
        os.environ["OPENAI_API_KEY"] = api_key
        os.environ["OPENAI_BASE_URL"] = base_url


def run(
    submission_path,
    paper_id,
    result_file,
    judge_type="simple",
    judge_model="azure_ai/gpt-5-mini",
    max_depth=999,
    code_dev=True,
    log_dir=None,
    data_dir=None,
):
    if not HAS_PAPERBENCH:
        raise ImportError(
            "paperbench and preparedness_turn_completer dependencies are missing. "
            "Please ensure they are installed in the Python environment to run the judge."
        )

    os.environ["PAPERBENCH_DATA_DIR"] = data_dir or os.environ.get(
        "PAPERBENCH_DATA_DIR", DEFAULT_DATA_DIR
    )

    # Dynamic litellm key and base mapping based on provider
    configure_litellm_for_model(judge_model)

    completer_model = judge_model
    base_url = os.environ.get("LITELLM_BASE_URL") or os.environ.get("LLM_BASE_URL")
    if "/" in completer_model and base_url:
        completer_model = completer_model.split("/", 1)[1]

    completer_config = None
    if judge_type == "simple" and OpenAICompletionsTurnCompleter is not None:
        completer_config = OpenAICompletionsTurnCompleter.Config(model=completer_model)

    submission_path = Path(submission_path)
    out_dir = Path(log_dir) if log_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    async def _run():
        paper = paper_registry.get_paper(paper_id)
        with open(paper.rubric, "r") as f:
            task_tree = TaskNode.from_dict(json.load(f))

        if code_dev:
            task_tree = task_tree.code_only() or task_tree.set_task_category(
                "Code Development"
            ).set_sub_tasks([])

        judge_kwargs = handle_judge_kwargs(
            judge_type, code_dev, paper, completer_config
        )

        # Pass structured completer configs so SimpleJudge doesn't fall back
        # to the hardcoded neulab/gpt-4o-2024-08-06 model.
        # Use reasoning_effort="low" and high max_tokens because the parsing
        # task is trivial and reasoning models waste output tokens on thinking.
        if judge_type == "simple" and completer_config is not None:
            judge_kwargs["float_completer_config"] = (
                OpenAICompletionsTurnCompleter.Config(
                    model=completer_model,
                    response_format=ParsedJudgeResponseFloat,
                    reasoning_effort="low",
                    max_tokens=4096,
                )
            )
            judge_kwargs["int_completer_config"] = (
                OpenAICompletionsTurnCompleter.Config(
                    model=completer_model,
                    response_format=ParsedJudgeResponseInt,
                    reasoning_effort="low",
                    max_tokens=4096,
                )
            )

        judge = create_judge(
            judge_type=judge_type,
            judge_kwargs=judge_kwargs,
            paper_path=paper.paper_pdf,
            rubric=task_tree,
            addendum=paper.addendum.read_text() if paper.addendum else None,
            judge_addendum=(
                paper.judge_addendum.read_text()
                if paper.judge_addendum.exists()
                else None
            ),
            submission_dir=submission_path,
            paper_md=paper.paper_md,
            log_path=out_dir,
            max_depth=max_depth,
        )
        return await judge.judge()

    graded_tree = asyncio.run(_run())

    token_usage = get_total_token_usage(graded_tree)
    total_cost = 0.0
    for model, usage in token_usage.to_dict().items():
        try:
            prompt_cost, completion_cost = cost_per_token(
                model=model,
                prompt_tokens=usage["in"],
                completion_tokens=usage["out"],
            )
            total_cost += prompt_cost + completion_cost
        except Exception:
            pass

    leaf_nodes = graded_tree.get_leaf_nodes()
    result = {
        "score": graded_tree.score,
        "num_nodes": len(leaf_nodes),
        "num_invalid_nodes": len([n for n in leaf_nodes if not n.valid_score]),
        "token_usage": token_usage.to_dict(),
        "cost": total_cost,
        "graded_task_tree": graded_tree.to_dict(),
    }

    with open(result_file, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Judge score: {result['score']}")
    print(f"Nodes: {result['num_nodes']}, Invalid: {result['num_invalid_nodes']}")
    print(f"Judge cost: ${total_cost:.4f}")
    print(f"Results saved to: {result_file}")


if __name__ == "__main__":
    fire.Fire(run)
