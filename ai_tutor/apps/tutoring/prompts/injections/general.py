"""General-subject injection — empty default.

When a course has no specific subject pack (e.g. geography, history,
language arts in the current curriculum), no extra rules are injected
beyond the provider-native base prompt. The base prompt is already
subject-agnostic and complete on its own.

If a non-math subject needs its own rules later (e.g. close-reading
prompts for language arts, lab safety for chemistry), add a new
sibling module and register it in `__init__.py::_REGISTRY`.
"""

INJECTION_ANTHROPIC = ""
INJECTION_GEMINI = ""
INJECTION_OPENAI = ""
