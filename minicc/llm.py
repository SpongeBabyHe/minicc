import os
from dotenv import load_dotenv
from anthropic import Anthropic
from pathlib import Path
from minicc.tools import TOOLS

load_dotenv()

WORKDIR = Path.cwd()
MODEL = os.environ["MODEL_ID"]
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
SYSTEM = f"You are a coding agent at {WORKDIR}. "


def llm_response(messages):
    response = client.messages.create(
        model=MODEL,
        messages=messages,
        max_tokens=8000,
        system=SYSTEM,
        tools=TOOLS
    )
    return response
