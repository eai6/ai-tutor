#!/bin/sh
# Re-apply phases 1 and 2 from the template state before either ran.
#
# A fix to the converter has to be retried against clean markup: applying it
# again over already-converted templates would layer utilities on utilities.
# c4b70bb is the commit before phase 1 touched a template.
set -e
BASE=c4b70bb

git checkout $BASE -- ai_tutor/templates ai_tutor/static/css
git checkout HEAD -- ai_tutor/static/css/app.build.css

venv/bin/python scripts/apply_map.py \
  --sheet ai_tutor/static/css/marketing/landing.css \
  --sheet ai_tutor/static/css/marketing/docs.css \
  --sheet ai_tutor/static/css/marketing/legal.css \
  --sheet ai_tutor/static/css/dashboard/layout.css \
  --sheet ai_tutor/static/css/dashboard/components/surfaces.css \
  --sheet ai_tutor/static/css/dashboard/components/controls.css \
  --sheet ai_tutor/static/css/dashboard/components/data.css \
  --sheet ai_tutor/static/css/dashboard/charts.css \
  --sheet ai_tutor/static/css/dashboard/pages/home.css \
  --sheet ai_tutor/static/css/dashboard/legacy.css \
  --templates ai_tutor/templates --apply

git rm -q -f --ignore-unmatch ai_tutor/static/css/marketing/*.css \
    ai_tutor/static/css/dashboard/layout.css ai_tutor/static/css/dashboard/charts.css \
    ai_tutor/static/css/dashboard/legacy.css ai_tutor/static/css/dashboard/components/*.css \
    ai_tutor/static/css/dashboard/pages/*.css
rmdir ai_tutor/static/css/marketing ai_tutor/static/css/dashboard/components \
      ai_tutor/static/css/dashboard/pages ai_tutor/static/css/dashboard 2>/dev/null || true

# After the deletion: fixups drops links whose stylesheet is now gone, which
# it can only decide once the files actually are.
venv/bin/python scripts/fixups.py

npm run css >/dev/null 2>&1
DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python manage.py collectstatic --noinput >/dev/null 2>&1
echo "phases 1+2 rebuilt"
