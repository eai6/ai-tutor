"""AI Tutor — a conversational tutoring platform for secondary schools.

Everything importable lives under this one package so the project can be
installed as a distribution. Before this existed, `apps` and `config` were
top-level names, which is fine for an application run from its own checkout and
unacceptable in site-packages, where they would collide with anything else.

Django app labels are unaffected: each AppConfig leaves `label` implicit, and
Django derives it from the last dotted component, so `ai_tutor.apps.tutoring`
still has the label `tutoring`. Existing databases need no migration.

Plan: memory/pip_package_plan.md
"""
