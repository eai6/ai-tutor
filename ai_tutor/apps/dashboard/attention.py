"""What needs the teacher, ranked.

The overview page used to open with three analytics figures. Analytics answer
"how did the term go"; a teacher opening the dashboard between two lessons is
asking "is anything wrong right now". This module turns the figures the view
already computes into a short, ordered list of things to act on, each one a
link to the page where the acting happens.

Design constraints:

* **No new queries.** Every input is a value ``dashboard_home`` has already
  computed. Adding a triage rail must not add a round trip to a page that
  already runs a dozen aggregates.
* **Ordered by urgency, not by category.** Safety outranks a drop-off, which
  outranks a soft score signal. The teacher reads top-left first.
* **Silence is a result.** An empty list means "nothing needs you", and the
  template says so explicitly rather than rendering an empty region.
* **Thresholds live here**, in one place, named — not scattered through a
  template as ``{% if x > 40 %}``.

Each item is a plain dict so the template tag stays presentation-only:

    {
        'key':    stable identifier, for tests,
        'tone':   danger | warning | info | success,
        'icon':   sprite name (no 'i-' prefix),
        'figure': the number the teacher scans,
        'label':  what it is,
        'detail': one line of context — why it matters or what to do,
        'url':    where acting on it happens,
    }
"""

from django.urls import reverse
from django.utils.translation import gettext_lazy as _


# --- Thresholds ------------------------------------------------------------
# Tuned against the Seychelles pilot's July numbers, where reach was ~59% and
# a 40% drop-off was the single largest source of missing assessment evidence.

#: Below this share of sessions reaching the exit ticket, drop-off is the
#: story of the window and gets surfaced.
REACH_FLOOR_PCT = 70

#: A cohort mean below this is a content problem, not a student problem.
SCORE_FLOOR_PCT = 60

#: Any student who went backwards between attempts is worth a look; more than
#: this many is a pattern.
DECLINED_FLOOR = 1

#: Below this share of the roster active in the window, the tool is not
#: reaching the class at all.
ENGAGEMENT_FLOOR_PCT = 50


def build_attention_items(
    *,
    flag_count=0,
    et=None,
    prog=None,
    total_students=0,
    active_students=0,
    flagged_url=None,
):
    """Return the ordered triage list for the overview page.

    All arguments are the values ``dashboard_home`` already has in hand.
    ``et`` and ``prog`` are the dicts from ``_exit_ticket_stats`` and
    ``_progression_stats``; both may be empty or missing keys, and every read
    below tolerates that — a dashboard must render even when a window has no
    data in it.
    """
    et = et or {}
    prog = prog or {}
    gain = prog.get('gain') or {}
    items = []

    # 1. Safety flags. Always first when present: it is the only item on this
    #    page with a duty-of-care attached to it.
    if flag_count:
        items.append({
            'key': 'safety_flags',
            'tone': 'danger',
            'icon': 'flag',
            'figure': flag_count,
            'label': _('flagged chats to review'),
            'detail': _('Safety judge flagged these conversations. Unreviewed.'),
            'url': flagged_url or reverse('dashboard:flagged_sessions'),
        })

    # 2. Students who went backwards. A declining student is invisible in a
    #    mean, and is the case most likely to need a person.
    declined = gain.get('declined') or 0
    if declined >= DECLINED_FLOOR:
        items.append({
            'key': 'declined',
            'tone': 'warning',
            'icon': 'trend-down',
            'figure': declined,
            'label': _('students scored lower on retry'),
            'detail': _('Their latest exit ticket is below their first. Check what changed.'),
            'url': reverse('dashboard:student_list'),
        })

    # 3. Drop-off before the exit ticket. Sessions that never reach it produce
    #    no assessment evidence at all, so this silently shrinks every other
    #    figure on the page.
    reach_pct = et.get('reach_pct')
    sessions_reached = et.get('sessions_reached') or 0
    if reach_pct is not None and reach_pct < REACH_FLOOR_PCT and sessions_reached:
        items.append({
            'key': 'drop_off',
            'tone': 'warning',
            'icon': 'exit-door',
            'figure': f'{100 - int(reach_pct)}%',
            'label': _('of sessions stopped before the exit ticket'),
            'detail': _('These lessons produced no score. Lesson length or difficulty is the usual cause.'),
            'url': reverse('dashboard:class_list'),
        })

    # 4. Cohort mean below the floor — a content signal rather than a
    #    per-student one, so it points at the curriculum.
    avg_pct = et.get('avg_pct')
    if avg_pct is not None and et.get('attempts') and avg_pct < SCORE_FLOOR_PCT:
        items.append({
            'key': 'low_scores',
            'tone': 'info',
            'icon': 'alert',
            'figure': f'{avg_pct}%',
            'label': _('mean exit ticket score'),
            'detail': _('Below the 60% floor across the class. Likely a lesson, not a cohort.'),
            'url': reverse('dashboard:curriculum_list'),
        })

    # 5. Roster not showing up. Last, because it is the slowest to act on.
    if total_students:
        engagement = round((active_students / total_students) * 100)
        if engagement < ENGAGEMENT_FLOOR_PCT:
            items.append({
                'key': 'low_engagement',
                'tone': 'info',
                'icon': 'students',
                'figure': f'{total_students - active_students}',
                'label': _('students had no session this period'),
                'detail': _('Out of %(total)s on the roster.') % {'total': total_students},
                'url': reverse('dashboard:student_list'),
            })

    return items
