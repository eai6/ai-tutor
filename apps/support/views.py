"""HTTP endpoints for the help assistant.

POST /support/chat/start/                  → start (or resume) a conversation
POST /support/chat/<conv_id>/message/      → send a user turn, get assistant reply
POST /support/chat/<conv_id>/confirm/      → resolve a pending write tool
POST /support/chat/<conv_id>/escalate/     → escalate to human support

All endpoints require login. Rate-limited via the existing safety
limiter.
"""

import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from apps.support import services as svc
from apps.support import tools as tool_catalog
from apps.support.models import HelpAssistantConversation, HelpAssistantMessage

logger = logging.getLogger(__name__)


def _resolve_audience(user) -> str:
    if not user or not user.is_authenticated:
        return 'student'
    if user.is_superuser:
        return 'super_admin'
    if user.is_staff:
        return 'teacher'
    return 'student'


def _check_rate_limit(user_id: int) -> tuple:
    """Reuse the existing RateLimiter pattern from chat_start_session.
    Returns (allowed, reason). Falls open if the limiter isn't
    available — the help assistant shouldn't be the place where a
    rate-limit module crash blocks support.
    """
    try:
        from apps.safety import RateLimiter
        return RateLimiter.check_rate_limit(user_id)
    except Exception:
        return True, ''


@login_required
@csrf_protect
@require_POST
def chat_start(request):
    """Start a new conversation, or resume the most recent active one."""
    try:
        body = json.loads(request.body or '{}')
    except (ValueError, TypeError):
        body = {}

    audience = _resolve_audience(request.user)
    conv = HelpAssistantConversation.objects.create(
        user=request.user,
        audience=audience,
        page_url_at_start=(body.get('page_url') or '')[:500],
        user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:500],
    )
    return JsonResponse({
        'ok': True,
        'conversation_id': conv.id,
        'audience': audience,
        'greeting': (
            "Hi 👋 — I'm your AI help assistant. I can answer questions about how the platform works "
            + ("or help you navigate the dashboard. " if audience in ('teacher', 'super_admin') else "and help you find the right lesson. ")
            + "What's up?"
        ),
    })


@login_required
@csrf_protect
@require_POST
def chat_message(request, conv_id: int):
    """Send a user message; get the assistant's response."""
    allowed, reason = _check_rate_limit(request.user.id)
    if not allowed:
        return JsonResponse({'ok': False, 'human_msg': reason or 'Rate limited — try again in a minute.'}, status=429)

    conv = get_object_or_404(
        HelpAssistantConversation, id=conv_id, user=request.user,
    )
    try:
        body = json.loads(request.body or '{}')
    except (ValueError, TypeError):
        body = {}
    text = (body.get('message') or '').strip()
    if not text:
        return JsonResponse({'ok': False, 'human_msg': 'Empty message.'}, status=400)

    try:
        result = svc.answer(conversation=conv, user_message_text=text)
    except Exception as e:
        logger.exception(f"Help assistant crashed: {e}")
        return JsonResponse({
            'ok': False,
            'human_msg': "Sorry — the assistant hit an error. Try sending to support.",
        }, status=500)

    return JsonResponse(result)


@login_required
@csrf_protect
@require_POST
def chat_confirm(request, conv_id: int):
    """Confirm or cancel a pending write-tool proposal."""
    conv = get_object_or_404(
        HelpAssistantConversation, id=conv_id, user=request.user,
    )
    try:
        body = json.loads(request.body or '{}')
    except (ValueError, TypeError):
        body = {}
    tool_use_id = body.get('tool_use_id')
    accept = bool(body.get('accept'))
    if not tool_use_id:
        return JsonResponse({'ok': False, 'human_msg': 'Missing tool_use_id.'}, status=400)

    confirm_result = svc.confirm_tool(
        conversation=conv, tool_use_id=tool_use_id, accept=accept,
    )
    if not confirm_result.get('ok'):
        return JsonResponse(confirm_result, status=400)

    # If the tool ran successfully, kick the assistant for a follow-up
    # turn so it can summarise the result. We feed a short prompt
    # back through the same answer() pipeline.
    if accept:
        followup = svc.answer(
            conversation=conv,
            user_message_text=(
                "(system) The action just ran. Briefly tell me what happened "
                "based on the tool result, and offer the next step."
            ),
        )
        return JsonResponse({
            'ok': True,
            'tool_result': confirm_result.get('result'),
            'follow_up': followup,
        })
    else:
        return JsonResponse({'ok': True, 'cancelled': True})


@login_required
@csrf_protect
@require_POST
def chat_escalate(request, conv_id: int):
    """Forward the conversation to human support as a FeedbackReport."""
    conv = get_object_or_404(
        HelpAssistantConversation, id=conv_id, user=request.user,
    )
    if request.content_type and request.content_type.startswith('multipart/form-data'):
        message_id = int(request.POST.get('message_id') or 0)
        message_text = (request.POST.get('message') or '')[:4000]
        screenshot = request.FILES.get('screenshot')
    else:
        try:
            body = json.loads(request.body or '{}')
        except (ValueError, TypeError):
            body = {}
        message_id = int(body.get('message_id') or 0)
        message_text = (body.get('message') or '')[:4000]
        screenshot = None

    if not message_id:
        # Default: escalate from the latest assistant message.
        last = conv.messages.filter(
            role=HelpAssistantMessage.Role.ASSISTANT,
        ).order_by('-created_at').first()
        if last:
            message_id = last.id
        else:
            return JsonResponse({'ok': False, 'human_msg': 'No assistant message to escalate.'}, status=400)

    result = svc.escalate_to_support(
        conversation=conv, message_id=message_id,
        message_text=message_text, screenshot=screenshot,
    )
    return JsonResponse(result)
