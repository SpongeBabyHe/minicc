"""Instruction used by the built-in ``/init`` command."""


# The skeleton is CC's official /init prompt, recovered verbatim from a CC
# session transcript during the three-arm /init experiment (2026-07-14). Three
# additions are retained from that experiment: verify-before-write,
# universal-quantifier checks, and batched independent reads.
INIT_PROMPT = (
    "Please analyze this codebase and create a CLAUDE.md file, which will be given "
    "to future AI coding agents (Claude Code, minicc) operating in this repository.\n\n"
    "What to add:\n"
    "1. Commands that will be commonly used, such as how to build, lint, and run "
    "tests. Include the necessary commands to develop in this codebase, such as "
    "how to run a single test.\n"
    "2. High-level code architecture and structure so that future instances can be "
    'productive more quickly. Focus on the "big picture" architecture that '
    "requires reading multiple files to understand.\n\n"
    "Usage notes:\n"
    "- VERIFY each command works by actually running it (bash) before documenting "
    "it; if it needs env vars or a .env file, say so next to the command. If you "
    "cannot verify a command, do not present it as working.\n"
    '- Before writing a claim containing "all", "only", "both" or '
    '"never", check each member it quantifies over.\n'
    "- Explore with glob/grep/read_file; batch independent file reads as parallel "
    "tool calls in a single turn — each extra turn re-reads the whole context.\n"
    "- If there's already a CLAUDE.md, improve it in place rather than duplicating.\n"
    "- Do not repeat yourself and do not include obvious instructions like "
    '"Provide helpful error messages to users", "Write unit tests for all new '
    'utilities", "Never include sensitive information (API keys, tokens) in '
    'code or commits".\n'
    "- Avoid listing every component or file structure that can be easily "
    "discovered.\n"
    "- Don't include generic development practices.\n"
    "- If there are Cursor rules (in .cursor/rules/ or .cursorrules) or Copilot "
    "rules (in .github/copilot-instructions.md), make sure to include the "
    "important parts.\n"
    "- If there is a README.md, make sure to include the important parts.\n"
    '- Do not make up information such as "Common Development Tasks", "Tips '
    'for Development", "Support and Documentation" unless this is expressly '
    "included in other files that you read.\n"
    "- Be sure to prefix the file with the following text:\n\n"
    "```\n"
    "# CLAUDE.md\n\n"
    "This file provides guidance to Claude Code (claude.ai/code) when working with "
    "code in this repository.\n"
    "```\n\n"
    "Write the result with write_file."
)
