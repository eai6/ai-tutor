#!/bin/sh
# Re-apply every phase from the template state before any of them ran.
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
  --sheet ai_tutor/static/css/student/brand.css \
  --sheet ai_tutor/static/css/student/shell.css \
  --sheet ai_tutor/static/css/student/components.css \
  --sheet ai_tutor/static/css/student/catalog.css \
  --sheet ai_tutor/static/css/student/auth.css \
  --sheet ai_tutor/static/css/student/legacy.css \
  --sheet ai_tutor/static/css/shared/flash.css \
  --sheet ai_tutor/static/css/shared/password-field.css \
  --sheet ai_tutor/static/css/shared/base.css \
  --templates ai_tutor/templates --apply

git rm -q -f --ignore-unmatch ai_tutor/static/css/marketing/*.css \
    ai_tutor/static/css/dashboard/layout.css ai_tutor/static/css/dashboard/charts.css \
    ai_tutor/static/css/dashboard/legacy.css ai_tutor/static/css/dashboard/components/*.css \
    ai_tutor/static/css/dashboard/pages/*.css \
    ai_tutor/static/css/student/*.css \
    ai_tutor/static/css/shared/flash.css ai_tutor/static/css/shared/password-field.css \
    ai_tutor/static/css/shared/base.css ai_tutor/static/css/shared/tokens.css
rmdir ai_tutor/static/css/marketing ai_tutor/static/css/dashboard/components \
      ai_tutor/static/css/dashboard/pages ai_tutor/static/css/dashboard \
      ai_tutor/static/css/student ai_tutor/static/css/shared 2>/dev/null || true

# After the deletion: fixups drops links whose stylesheet is now gone, which
# it can only decide once the files actually are.
# The templates' own <style> blocks are the other half of the stylesheets.
venv/bin/python scripts/inline_styles.py --apply | tail -1

venv/bin/python scripts/fixups.py

npm run css >/dev/null 2>&1
DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python manage.py collectstatic --noinput >/dev/null 2>&1
echo "all phases rebuilt"
