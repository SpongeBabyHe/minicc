import os
from dotenv import load_dotenv
from anthropic import Anthropic
from pathlib import Path
from minicc.tools import TOOLS
from minicc.prompts.system import build_system_prompt

load_dotenv()

_USAGE = {"input": 0, "output": 0}


MODEL = os.environ["MODEL_ID"]
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
SYSTEM = build_system_prompt()


def llm_response(messages):
    response = client.messages.create(
        model=MODEL,
        messages=messages,
        max_tokens=8000,
        system=SYSTEM,
        tools=TOOLS
    )
    _USAGE["input"] = response.usage.input_tokens
    _USAGE["output"] = response.usage.output_tokens
    return response


def get_usage() -> dict:
    """Cumulative token usage since process start."""
    return dict(_USAGE)
