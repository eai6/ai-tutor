"""Terminal client for the simple-tutor engine.

Exists so the tutor can be driven from a shell instead of a browser. On the
Jetson that is not a convenience: the box has 7.4 GB of unified CPU/GPU memory,
the local 4B needs ~4.0 GB resident, and Firefox was measured holding ~1.3 GB —
with the browser open the Ollama fit preflight refuses to load the model at all.

Plan: memory/terminal_tutor_client_plan.md
"""
