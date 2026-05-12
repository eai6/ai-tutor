"""Tutor evaluation benchmark — see memory/eval_benchmark_v2_simplified.md.

Frozen-snapshot benchmark over real Seychelles pilot session traces.
Captures conversation history + production tutor response + pipeline trace
at sampling time; per-(item, system_variant, annotator) annotations record
actual labels, expected labels, rationale, and a computed verdict.

Powers iteration on tutor prompts and judges:
- Sample once → annotate once → score baseline.
- Each system variant (prompt change, judge tuning) re-runs the same items
  and gets scored against the locked rubric.
- Pass rate sliced by subject + eval_layer drives prompt-engineering work.
"""
default_app_config = 'apps.benchmark.apps.BenchmarkConfig'
