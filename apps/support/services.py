"""Help-assistant orchestration: retrieval → LLM → tool dispatch.

Anthropic-specific (the help_assistant ModelConfig purpose
defaults to Haiku 4.5). Other providers can be added later.

See memory/help_assistant_plan.md.
"""

import json
import logging
from typing import Dict, List, Optional, Tuple

from django.utils import timezone

from apps.support.kb import HelpKB
from apps.support import tools as tool_catalog
from apps.support.models import (
    HelpAssistantConversation,
    HelpAssistantMessage,
    HelpAssistantToolCall,
)

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the AI Tutor platform's help assistant. You support both students and teachers (and occasional super-admin users) on a Django-based tutoring platform.

CRITICAL RULES:
- Answer ONLY using the documentation snippets provided in <docs>...</docs>. If the snippets don't cover the question, say so plainly and suggest sending the question to human support.
- You may NEVER suggest deleting, removing, or destroying anything (students, courses, lessons, attempts, accounts, etc). If a user asks to delete something, direct them to the dashboard's delete button — do not propose a tool call. The tool catalog cannot do destructive things; refuse politely if asked.
- When you can answer, be terse: 2–4 sentences plus a clear next step. End by citing the section title in parentheses, e.g. "(from Help → Exit tickets)".
- When a tool can take a useful action (e.g. recommend a lesson, link to a page, assign a lesson), prefer the tool over a long explanation. Confirm before any write action — the UI will show a confirmation card before executing.
- If multiple courses / students / lessons match a fuzzy query, ask the user to disambiguate by listing the candidates.
- Tone: warm, action-oriented, no filler.

CONTEXT:
- audience: {audience}
- page_url_at_start: {page_url}
"""


def _resolve_model_config():
    """Pick the Anthropic model config for the help_assistant
    purpose. Falls back to the tutoring config if a help-specific
    one isn't registered yet."""
    from apps.llm.models import ModelConfig
    cfg = ModelConfig.get_for('help_assistant') or ModelConfig.get_for('tutoring')
    if not cfg:
        raise ValueError("No active LLM model configured for the help assistant.")
    return cfg


def _build_history_for_anthropic(conversation: HelpAssistantConversation) -> List[Dict]:
    """Convert HelpAssistantMessage rows into Anthropic message format.

    Anthropic expects alternating user/assistant blocks, with tool
    results threaded inside user blocks. Tool-result rows in our DB
    are a separate row type; they ride alongside the matching tool
    proposal.
    """
    out: List[Dict] = []
    msgs = list(conversation.messages.order_by('created_at'))
    for m in msgs:
        if m.role == HelpAssistantMessage.Role.USER:
            out.append({'role': 'user', 'content': m.content})
        elif m.role == HelpAssistantMessage.Role.ASSISTANT:
            blocks: List[Dict] = []
            if m.content:
                blocks.append({'type': 'text', 'text': m.content})
            for prop in (m.tool_proposals or []):
                blocks.append({
                    'type': 'tool_use',
                    'id': prop.get('tool_use_id'),
                    'name': prop.get('name'),
                    'input': prop.get('inputs') or {},
                })
            if blocks:
                out.append({'role': 'assistant', 'content': blocks})
        elif m.role == HelpAssistantMessage.Role.TOOL_RESULT:
            # Tool result rows have content = JSON of {tool_use_id, result}.
            try:
                payload = json.loads(m.content or '{}')
            except (ValueError, TypeError):
                payload = {}
            out.append({
                'role': 'user',
                'content': [{
                    'type': 'tool_result',
                    'tool_use_id': payload.get('tool_use_id'),
                    'content': json.dumps(payload.get('result') or {}),
                }],
            })
    return out


def _retrieve_docs(question: str, audience: str) -> List[Dict]:
    chunks = HelpKB().query(question, audience=audience, n_results=5)
    return chunks


def _format_docs_for_prompt(chunks: List[Dict]) -> str:
    if not chunks:
        return "<docs>(no matching documentation found)</docs>"
    parts = ["<docs>"]
    for i, c in enumerate(chunks, 1):
        parts.append(f"[{i}] (Help & FAQ → {c['section_title']})")
        parts.append(c['text'])
        parts.append('')
    parts.append("</docs>")
    return '\n'.join(parts)


def answer(*, conversation: HelpAssistantConversation,
           user_message_text: str) -> Dict:
    """Run one turn of the assistant.

    1. Save the user's message to the DB.
    2. Retrieve relevant doc chunks.
    3. Call Anthropic with the conversation history + tools.
    4. If the model wants to use a tool:
        - read-only: dispatch immediately, feed result back, continue
          the loop until the model emits text.
        - write: persist a tool proposal on the assistant message,
          return early — the UI will show a confirmation card.
    5. Save the assistant message + citations.

    Returns a dict shaped for the JSON response.
    """
    audience = conversation.audience
    cfg = _resolve_model_config()
    if cfg.provider != 'anthropic':
        return {
            'ok': False,
            'human_msg': "Help assistant currently requires an Anthropic model. Configure one in Settings → LLM Models.",
        }

    # 1. Persist the user message.
    user_msg = HelpAssistantMessage.objects.create(
        conversation=conversation,
        role=HelpAssistantMessage.Role.USER,
        content=user_message_text[:8000],
    )

    # 2. Retrieve docs (only on the first turn or when the user
    # message looks like a fresh question — for v1, always retrieve).
    chunks = _retrieve_docs(user_message_text, audience)

    # 3. Compose the system prompt + the conversation history.
    system = SYSTEM_PROMPT.format(
        audience=audience,
        page_url=conversation.page_url_at_start or '(unknown)',
    )
    docs_block = _format_docs_for_prompt(chunks)

    # Inject the docs block as a system-level addendum on each turn.
    # Anthropic supports a list of system blocks; we keep it simple
    # with one concatenated string.
    system_with_docs = system + "\n\n" + docs_block

    history = _build_history_for_anthropic(conversation)
    # The user message we just persisted is already in history via
    # _build_history_for_anthropic. Sanity check:
    if not history or history[-1].get('role') != 'user':
        history.append({'role': 'user', 'content': user_message_text[:8000]})

    tools_for_model = tool_catalog.llm_tool_specs(audience)

    # 4. LLM call (with tool-use loop for read-only tools).
    try:
        from anthropic import Anthropic
    except ImportError:
        return {'ok': False, 'human_msg': 'Anthropic SDK not installed on this deploy.'}

    client = Anthropic(api_key=cfg.get_api_key())

    assistant_text_parts: List[str] = []
    pending_proposals: List[Dict] = []  # write tools awaiting user confirm

    # Hard cap on tool iterations to avoid runaway loops.
    for _iter in range(6):
        kwargs = {
            'model': cfg.model_name,
            'max_tokens': 1024,
            'system': system_with_docs,
            'messages': history,
        }
        if tools_for_model:
            kwargs['tools'] = tools_for_model

        try:
            resp = client.messages.create(**kwargs)
        except Exception as e:
            logger.error(f"Anthropic call failed: {e}")
            HelpAssistantMessage.objects.create(
                conversation=conversation,
                role=HelpAssistantMessage.Role.ASSISTANT,
                content=f"Sorry — I hit an error reaching the model. Try again, or send to support.",
            )
            return {
                'ok': False,
                'human_msg': str(e)[:500],
                'error': 'llm_call_failed',
            }

        # Walk response blocks. Two cases per block: 'text' or 'tool_use'.
        any_tool_use = False
        new_history_assistant_blocks: List[Dict] = []
        for block in resp.content:
            block_type = getattr(block, 'type', None)
            if block_type == 'text':
                assistant_text_parts.append(block.text)
                new_history_assistant_blocks.append({'type': 'text', 'text': block.text})
            elif block_type == 'tool_use':
                any_tool_use = True
                tool_name = block.name
                tool_use_id = block.id
                tool_input = block.input or {}
                new_history_assistant_blocks.append({
                    'type': 'tool_use',
                    'id': tool_use_id,
                    'name': tool_name,
                    'input': tool_input,
                })

                spec = tool_catalog.get_tool(tool_name)
                if not spec:
                    history.append({'role': 'assistant', 'content': new_history_assistant_blocks})
                    history.append({
                        'role': 'user',
                        'content': [{
                            'type': 'tool_result',
                            'tool_use_id': tool_use_id,
                            'content': json.dumps({'error': 'unknown_tool'}),
                        }],
                    })
                    new_history_assistant_blocks = []
                    continue

                if spec.get('requires_confirmation'):
                    # Park the proposal — UI will show a confirm card.
                    pending_proposals.append({
                        'tool_use_id': tool_use_id,
                        'name': tool_name,
                        'inputs': tool_input,
                        'requires_confirmation': True,
                    })
                    # Don't advance the conversation past this proposal —
                    # we'll resume after the user confirms or cancels.
                    continue

                # Read-only: dispatch inline.
                handler = spec['handler']
                try:
                    result = handler(conversation.user, **tool_input) or {}
                except Exception as e:
                    logger.exception(f"Tool {tool_name} crashed: {e}")
                    result = {'ok': False, 'error': 'handler_crashed', 'human_msg': str(e)[:200]}

                # Audit row.
                HelpAssistantToolCall.objects.create(
                    conversation=conversation,
                    message=user_msg,  # provisional; we'll re-link after persisting assistant msg
                    user=conversation.user,
                    tool_name=tool_name,
                    inputs=tool_input,
                    status=(
                        HelpAssistantToolCall.Status.EXECUTED
                        if result.get('ok')
                        else HelpAssistantToolCall.Status.ERRORED
                    ),
                    requires_confirmation=False,
                    executed_at=timezone.now(),
                    result=result,
                    error=str(result.get('error', ''))[:1000],
                )

                # Feed result back into history and loop.
                history.append({'role': 'assistant', 'content': new_history_assistant_blocks})
                history.append({
                    'role': 'user',
                    'content': [{
                        'type': 'tool_result',
                        'tool_use_id': tool_use_id,
                        'content': json.dumps(result),
                    }],
                })
                new_history_assistant_blocks = []
                # Continue inner loop to grab more blocks if any.

        # If no tool use this turn, we're done — append final
        # assistant blocks to history conceptually, then break.
        if not any_tool_use:
            break

        # If there were pending writes and no read-only tool advanced,
        # the loop should also exit so we can return the proposals.
        if pending_proposals and not any_tool_use:
            break

        # If we have unconsumed assistant blocks (e.g. text alongside
        # a write tool), still need them in history for next iter.
        if new_history_assistant_blocks:
            history.append({'role': 'assistant', 'content': new_history_assistant_blocks})

        # Stop if no more text + only pending writes.
        if pending_proposals and not any_tool_use:
            break

    final_text = '\n'.join(assistant_text_parts).strip()

    # 5. Persist the assistant message.
    citations = [
        {
            'source': c['source'],
            'section_title': c['section_title'],
            'anchor': c.get('anchor', ''),
        }
        for c in chunks
    ]
    asst_msg = HelpAssistantMessage.objects.create(
        conversation=conversation,
        role=HelpAssistantMessage.Role.ASSISTANT,
        content=final_text or '',
        citations=citations,
        tool_proposals=pending_proposals,
    )

    # Re-link any tool-call audit rows from this turn to the
    # assistant message that proposed them.
    HelpAssistantToolCall.objects.filter(
        conversation=conversation, message=user_msg,
    ).update(message=asst_msg)

    # Persist pending-confirmation proposals as audit rows in
    # 'proposed' state so we can resolve on /confirm/.
    for prop in pending_proposals:
        HelpAssistantToolCall.objects.create(
            conversation=conversation,
            message=asst_msg,
            user=conversation.user,
            tool_name=prop['name'],
            inputs=prop['inputs'],
            status=HelpAssistantToolCall.Status.PROPOSED,
            requires_confirmation=True,
        )

    return {
        'ok': True,
        'message_id': asst_msg.id,
        'content': final_text,
        'citations': citations,
        'pending_tool_proposals': pending_proposals,
    }


def confirm_tool(*, conversation: HelpAssistantConversation,
                 tool_use_id: str, accept: bool) -> Dict:
    """Resolve a pending write-tool proposal. If accept=True, run
    the handler and write a TOOL_RESULT message. If False, mark
    cancelled and write a "cancelled" tool result so the LLM can
    move on.
    """
    audit = HelpAssistantToolCall.objects.filter(
        conversation=conversation,
        status=HelpAssistantToolCall.Status.PROPOSED,
        requires_confirmation=True,
    ).order_by('-proposed_at').first()
    # Find the matching proposal by tool_use_id stored on the
    # owning assistant message.
    target_audit = None
    for a in conversation.tool_calls.filter(
        status=HelpAssistantToolCall.Status.PROPOSED,
    ):
        for prop in (a.message.tool_proposals or []):
            if prop.get('tool_use_id') == tool_use_id:
                target_audit = a
                break
        if target_audit:
            break

    if not target_audit:
        return {'ok': False, 'human_msg': 'Pending action not found or already resolved.'}

    if not accept:
        target_audit.status = HelpAssistantToolCall.Status.CANCELLED
        target_audit.cancelled_at = timezone.now()
        target_audit.save(update_fields=['status', 'cancelled_at'])
        # Feed a cancelled tool_result into the conversation history.
        HelpAssistantMessage.objects.create(
            conversation=conversation,
            role=HelpAssistantMessage.Role.TOOL_RESULT,
            content=json.dumps({
                'tool_use_id': tool_use_id,
                'result': {'ok': False, 'cancelled': True,
                           'human_msg': 'User cancelled the action.'},
            }),
        )
        return {'ok': True, 'cancelled': True}

    # Execute.
    spec = tool_catalog.get_tool(target_audit.tool_name)
    if not spec:
        target_audit.status = HelpAssistantToolCall.Status.ERRORED
        target_audit.error = 'unknown_tool'
        target_audit.save(update_fields=['status', 'error'])
        return {'ok': False, 'human_msg': 'Tool no longer available.'}

    target_audit.status = HelpAssistantToolCall.Status.CONFIRMED
    target_audit.confirmed_at = timezone.now()
    target_audit.save(update_fields=['status', 'confirmed_at'])

    handler = spec['handler']
    try:
        result = handler(conversation.user, **(target_audit.inputs or {})) or {}
    except Exception as e:
        logger.exception(f"Tool {target_audit.tool_name} crashed during confirm: {e}")
        result = {'ok': False, 'human_msg': str(e)[:200]}
        target_audit.status = HelpAssistantToolCall.Status.ERRORED
        target_audit.error = str(e)[:1000]
        target_audit.executed_at = timezone.now()
        target_audit.result = result
        target_audit.save()
        # Continue — feed the error back to the LLM.
    else:
        target_audit.status = (
            HelpAssistantToolCall.Status.EXECUTED
            if result.get('ok')
            else HelpAssistantToolCall.Status.ERRORED
        )
        target_audit.executed_at = timezone.now()
        target_audit.result = result
        target_audit.error = str(result.get('error', ''))[:1000]
        target_audit.save()

    # Persist a TOOL_RESULT message so the next turn's history
    # picks it up.
    HelpAssistantMessage.objects.create(
        conversation=conversation,
        role=HelpAssistantMessage.Role.TOOL_RESULT,
        content=json.dumps({'tool_use_id': tool_use_id, 'result': result}),
    )

    return {'ok': True, 'result': result}


def escalate_to_support(*, conversation: HelpAssistantConversation,
                        message_id: int, message_text: str = '',
                        screenshot=None) -> Dict:
    """Forward the conversation to human support. Creates a
    FeedbackReport with the transcript pre-pasted, links the
    assistant message to it.
    """
    from apps.dashboard.models import FeedbackReport

    asst_msg = HelpAssistantMessage.objects.filter(
        id=message_id, conversation=conversation,
    ).first()
    if not asst_msg:
        return {'ok': False, 'human_msg': 'Message not found.'}

    transcript_lines = []
    for m in conversation.messages.order_by('created_at'):
        prefix = m.role.upper()
        transcript_lines.append(f"[{prefix}] {m.content[:1000]}")
    transcript = '\n\n'.join(transcript_lines)

    user_addition = (message_text or '').strip()
    body = (user_addition + '\n\n---\nHELP CHAT TRANSCRIPT:\n' + transcript).strip()

    inst = None
    try:
        from apps.accounts.models import Membership
        m = Membership.objects.filter(user=conversation.user, is_active=True).first()
        if m:
            inst = m.institution
    except Exception:
        pass

    report = FeedbackReport.objects.create(
        user=conversation.user,
        institution=inst,
        kind=FeedbackReport.Kind.BUG,
        severity=FeedbackReport.Severity.MEDIUM,
        message=body[:4000],
        page_url=conversation.page_url_at_start or '',
        user_agent=conversation.user_agent or '',
        screenshot=screenshot,
    )
    asst_msg.escalated_to_feedback = report
    asst_msg.save(update_fields=['escalated_to_feedback'])
    return {'ok': True, 'feedback_id': report.id}
