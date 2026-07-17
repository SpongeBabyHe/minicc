"""The `skill` tool: the model-side entry to skills (CC's Skill tool).

The tool description is STATIC (cache-friendly — it lives in the tools+system
prefix layer); the always-in-context skill listing arrives as a
<system-reminder> in the conversation instead, exactly like CC ("Available
skills appear in a system-reminder listing" — CC's own tool description).
reminders.py injects it and re-injects on change; this module only resolves
and renders. Invoking returns the rendered SKILL.md content as the tool
result; the model follows it in place of its default approach.

Ungated: invoking a skill only loads user-authored instructions into context
(CC doesn't prompt for Skill either). Anything the skill then DOES — bash,
edits — still passes the normal permission gate, minus whatever the skill's
own allowed-tools granted for the turn (see PERMISSIONS.md / SKILL_DESIGN.md).
"""

from minicc import skills

# The description is CC's own Skill-tool text, observed verbatim in a live CC
# session (2026-07-16) — the /init precedent: port word-for-word, drop only the
# sentences about absent infra (plugin `plugin:skill` namespacing,
# directory-scoped `apps/web:deploy` names, subagent-run skills — all in
# SKILL_DESIGN.md's cuts ledger). Built-in command names adapted to minicc's.
SCHEMA = {
    "name": "skill",
    "description": (
        "Invoke a skill.\n\n"
        "A skill is a packaged set of instructions the user or project has "
        "set up for a particular kind of task (deploy steps, a review "
        "checklist, a repo-specific workflow). Available skills appear in a "
        "system-reminder listing with one-line descriptions. When the task at "
        "hand is one a listed skill covers, call this tool first — the "
        "skill's instructions load into the turn for you to follow in place "
        "of your default approach. Users may also ask for one by name "
        "(`/<name>`, or \"slash command\"); that's a request to invoke it.\n\n"
        "Only names from the listing (or that the user typed explicitly) are "
        "valid. Built-in commands (`/help`, `/clear`, `/compact`, …) aren't "
        "skills. If a `<command-name>` block is already present this turn, "
        "the skill is loaded — follow it directly rather than calling again."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "Exact name from the listing, no leading slash.",
            },
            "args": {
                "type": "string",
                "description": "Optional arguments to pass through.",
            },
        },
        "required": ["skill"],
    },
}


def skill(skill: str, args: str = ""):
    sk = skills.lookup(skill)
    if sk is None:
        return (
            f"Error: no skill named '{skill}'. Valid names are in the "
            "skill listing (a <system-reminder> in this conversation); "
            "do not guess."
        )
    if sk.flag("disable-model-invocation"):
        # CC hides these from the listing AND blocks programmatic invocation.
        return (
            f"Error: skill '{skill}' can only be invoked by the user "
            f"(they type /{skill})."
        )
    content, dup = skills.render_tracked(sk, args or "")
    skills.apply_grants(sk)  # allowed-tools stay active until the next user prompt
    if dup:
        return (
            f"Skill '{skill}' is already loaded — its rendered content is "
            "unchanged from the copy earlier in this conversation. Follow "
            "those instructions."
        )
    # CC's tool_result shape, probed live (2026-07-17): the base-dir header
    # precedes the body on the model path too (no command tags — those are
    # the user path's envelope).
    return f"Base directory for this skill: {sk.dir}\n\n{content}"
