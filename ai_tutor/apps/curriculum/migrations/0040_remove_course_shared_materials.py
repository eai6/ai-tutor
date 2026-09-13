"""Drop Course.shared_materials.

Added in 0039 as a hand-attach escape hatch for platform-wide materials, and
removed the same day: it duplicated the course page's own "Upload Material"
button, and the thing actually wanted was for the automatic subject+grade
inheritance to work — which is a matching problem, not a second mechanism.

Dropping the join table outright rather than deprecating it in state first
(the pattern 0037/0038 use for the columns they retire): nothing outside this
feature ever wrote to it, and it shipped and was removed within hours, so
there is no data to preserve for a rollback.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [('curriculum', '0039_add_course_shared_materials')]

    operations = [
        migrations.RemoveField(model_name='course', name='shared_materials'),
    ]
