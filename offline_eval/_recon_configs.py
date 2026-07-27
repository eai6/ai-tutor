"""Recon: print the active ModelConfig rows that matter for the offline eval."""
import os, sys, django
ROOT = os.environ.get('AI_TUTOR_ROOT') or '/home/daniel/Documents/work/Nyansapo/web/ai-tutor'
os.chdir(ROOT); sys.path.insert(0, ROOT)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from apps.llm.models import ModelConfig

print("=== ALL ModelConfig rows (purpose | provider | model | active | inst) ===")
for c in ModelConfig.objects.all().order_by('purpose', '-is_active', '-updated_at'):
    inst = c.institution.slug if c.institution_id else 'platform'
    print(f"  {c.purpose:<28} {c.provider:<14} {c.model_name:<34} active={c.is_active!s:<5} inst={inst}")

print("\n=== get_for() resolution for the eval-critical purposes ===")
for p in ['tutoring', 'judge', 'student_sim', 'generation']:
    try:
        c = ModelConfig.get_for(p)
        if c:
            print(f"  {p:<14} -> {c.provider}/{c.model_name} (active={c.is_active})")
        else:
            print(f"  {p:<14} -> None")
    except Exception as e:
        print(f"  {p:<14} -> ERR {e}")
