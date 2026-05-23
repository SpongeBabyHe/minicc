# minicc

A tiny coding agent CLI — a from-scratch reimplementation of Claude Code's core loop, built as a learning project.

## Status

Walking skeleton. Currently at M1: agent loop + `bash` tool.

## Run

```bash
cp .env.example .env   # add your ANTHROPIC_API_KEY
pip install -e .
python -m minicc.agent
```