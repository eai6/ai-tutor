"""Known model names per provider, for the settings picker.

The settings page used to ask an administrator to type a model id into a free
text box — `claude-sonnet-4-20250514`, exactly, from memory, into a field too
narrow to show the whole string. A typo there does not fail loudly: the config
saves, and the next tutoring call is the thing that breaks.

This is a *suggestion* list, not a whitelist. Nothing validates against it and
the settings form still accepts any string, because model ids change faster
than this file will: the picker carries an "Other" option that reveals the
text box again. Adding a model here only saves someone the typing.

Every id below is one this repository already references — in the llm app's
migrations, or in the defaults the settings view shipped with. Do not add ids
here speculatively; an id in this list reads as "this works", and a
hallucinated one costs a debugging session.
"""

from __future__ import annotations

# provider -> [(model_id, human label)]. First entry is that provider's
# default, used when someone switches provider without picking a model.
TEXT_MODELS: dict[str, list[tuple[str, str]]] = {
    'anthropic': [
        ('claude-opus-4-7', 'Claude Opus 4.7'),
        ('claude-sonnet-4-20250514', 'Claude Sonnet 4'),
        ('claude-haiku-4-5-20251001', 'Claude Haiku 4.5'),
    ],
    'openai': [
        ('gpt-4o', 'GPT-4o'),
        ('gpt-4o-mini', 'GPT-4o mini'),
    ],
    'google': [
        ('gemini-3.1-pro-preview', 'Gemini 3.1 Pro (preview)'),
        ('gemini-3.1-flash-lite-preview', 'Gemini 3.1 Flash Lite (preview)'),
        ('gemini-3.5-flash', 'Gemini 3.5 Flash'),
        ('gemini-2.5-flash', 'Gemini 2.5 Flash'),
    ],
    'azure_openai': [
        ('gpt-4o', 'GPT-4o'),
    ],
    'local_ollama': [
        ('llama3', 'Llama 3'),
        ('qwen-3-5-2b-q4-k-m', 'Qwen 3.5 2B (q4_k_m)'),
    ],
    # Vertex serves third-party weights through Model Garden; the ids are
    # deployment-specific, so there is nothing useful to suggest.
    'vertex_model_garden': [],
}

#: Image generation is a separate list — a text model in an image slot fails
#: at call time with an error that does not mention the settings page.
IMAGE_MODELS: dict[str, list[tuple[str, str]]] = {
    'openai': [
        ('gpt-image-2', 'GPT Image 2'),
    ],
    'google': [
        ('gemini-3.1-flash-image-preview', 'Gemini 3.1 Flash Image (preview)'),
    ],
}


def as_json_dict(catalog: dict[str, list[tuple[str, str]]]) -> dict[str, list[list[str]]]:
    """Shape for `json_script` — tuples are not JSON, lists are."""
    return {provider: [list(pair) for pair in models]
            for provider, models in catalog.items()}


def default_for(provider: str, catalog: dict[str, list[tuple[str, str]]] | None = None) -> str:
    """The model to pre-fill when someone picks *provider*.

    Empty string when the provider has no suggestions, which the form treats
    as "type one in" rather than silently saving a blank.
    """
    models = (catalog if catalog is not None else TEXT_MODELS).get(provider) or []
    return models[0][0] if models else ''


def defaults(catalog: dict[str, list[tuple[str, str]]]) -> dict[str, str]:
    """provider -> default model id, for every provider in *catalog*."""
    return {provider: default_for(provider, catalog) for provider in catalog}
