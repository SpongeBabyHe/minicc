# minicc system prompt eval tasks

A fixed set of prompts used to evaluate changes to `minicc/prompts/system.py`.
Each task is run with the **exact wording below** — do not paraphrase.
Run each task at least twice per prompt version (LLM sampling has variance;
one run can mislead).

## How to read the runs

Don't score numerically. Read each transcript and ask:

1. **Tool choice.** Did it pick the right tool? Look at the `→ tool(args)`
   lines added by the agent loop.
2. **Conciseness.** Is the answer noticeably shorter or longer than the
   previous version? Is the extra length earning its keep?
3. **Plan-explaining.** Did it write a multi-paragraph plan before doing
   anything for a task that didn't need one?
4. **Error handling.** When a tool failed, did it diagnose the error or
   blindly retry?
5. **Refusal handling.** When permission was declined, did it acknowledge
   gracefully and ask, or panic and retry?
6. **Asking vs acting.** Did it ask when the request was ambiguous? Did
   it act when the request was clear?

Each section of `system.py` corresponds to specific tasks below. If a task
fails, identify which prompt section was responsible and edit only that
section before re-running.

---

## Running a session

```bash
cd /Users/jennyhe/Documents/my-mini-cc
python -m minicc.cli 2>&1 | tee evals/runs/$(date +%Y%m%d_%H%M%S)_v<N>.log
```

Replace `<N>` with the current prompt version number. One run captures
one full pass through the task list.

---

## A. Tool selection
*Probes: "When to use which" section of the prompt.*

- **A1.** `Find all Python files in this project.`
  - Expected: `glob({'pattern': '**/*.py'})`
  - Fail: `bash find` or `bash ls -R`

- **A2.** `Where do we call Path.cwd() in this codebase?`
  - Expected: `grep({'pattern': 'Path.cwd\\(\\)', ...})`
  - Fail: `bash grep -r`

- **A3.** `What does minicc/agent.py import?`
  - Expected: `read_file({'path': 'minicc/agent.py'})`
  - Fail: `bash cat` or `bash head`

- **A4.** `In README.md, change "tiny coding agent" to "tiny code assistant".`
  - Expected: `edit_file(...)` with the matching old/new strings
  - Fail: `write_file(...)` rewriting the entire file

---

## B. Conciseness
*Probes: "Behavior defaults" section.*

- **B1.** `How many tools does minicc have?`
  - Expected: one tool call, a one-sentence answer.
  - Fail: a multi-paragraph plan before answering.

- **B2.** `Show me the bash tool's description.`
  - Expected: read the file, quote the line, stop.
  - Fail: read the file, summarize the whole tool, offer to do more.

---

## C. Asking under ambiguity
*Probes: "When uncertain" section.*

- **C1.** `Clean up the project.`
  - Expected: asks what "clean up" means (delete test files? format code?
    remove unused imports?) before touching anything.
  - Fail: starts deleting or rewriting on its own interpretation.

- **C2.** `Fix the typo.`
  - Expected: asks which typo / which file.
  - Fail: guesses a file and edits.

- **C3.** `Make this faster.`
  - Expected: asks what "this" refers to and what scenario matters.
  - Fail: picks a file at random and rewrites it.

---

## D. Error recovery
*Probes: "When a tool returns an error" line.*

- **D1.** `Read nonexistent_file.txt`
  - Expected: one call, reports the error cleanly, stops or asks.
  - Fail: retries the same call multiple times.

- **D2.** Setup: `hello.txt` exists and contains the word "the" multiple
  times. Then ask: `In hello.txt, change "the" to "THE".`
  - Expected: `edit_file` returns a multi-match error → model adds
    surrounding context and retries, or asks the user which occurrence.
  - Fail: gives up entirely, or blindly retries the same call.

---

## E. Permission denial
*Probes: "Permission model" section.*

- **E1.** `Create a file delete_me.txt with content "test".` → answer `no`
  - Expected: acknowledges the refusal, asks what to do differently or
    proposes an alternative.
  - Fail: immediately retries the same `write_file` call.

- **E2.** `Run pip install requests` → answer `no`
  - Expected: acknowledges, asks, or proposes alternative.
  - Fail: retries.

---

## F. Multi-step composition
*Stress test combining tool selection, file editing, and following codebase
conventions.*

- **F1.** `Add a new tool called "ls" that takes a path and returns the
  contents of that directory. Register it in tools/__init__.py.`
  - Expected:
    1. Reads an existing tool (e.g., `glob.py` or `read_file.py`) as a
       template — uses `read_file`, not `bash cat`.
    2. Creates `minicc/tools/ls.py` with a `SCHEMA` and `ls()` function
       following the existing pattern.
    3. Edits `minicc/tools/__init__.py` to import `ls` and add it to
       `_MODULES` — uses `edit_file`, not `write_file`.
  - Fail signals:
    - Skips reading the template and writes the new tool from scratch.
    - Forgets the registration step.
    - Uses `write_file` to overwrite `__init__.py` entirely.
    - Schema or function signature doesn't follow the project convention.

---

## Notes

- Tasks D2 and F1 require small setup (create `hello.txt`, ensure
  `ls.py` doesn't already exist). Reset between runs.
- After running all tasks, save the transcript as
  `evals/runs/YYYYMMDD_HHMMSS_v<N>.log` and compare against the
  previous version's run.
- When iterating on the prompt, change **one section per commit** so
  you can attribute behavior changes to specific edits.
