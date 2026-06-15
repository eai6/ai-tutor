"""Probe: does an Ollama model actually emit tool_calls? Decisive for whether
simple_tutor (which REQUIRES tool-use) can run on OSS models at all."""
import json
import sys
import requests

MODEL = sys.argv[1] if len(sys.argv) > 1 else 'qwen2.5:0.5b'
TOOLS = [{
    'type': 'function',
    'function': {
        'name': 'pose_question',
        'description': 'Pose a question to the student.',
        'parameters': {
            'type': 'object',
            'properties': {
                'question_text': {'type': 'string'},
                'question_type': {'type': 'string'},
            },
            'required': ['question_text'],
        },
    },
}]
payload = {
    'model': MODEL,
    'messages': [
        {'role': 'system', 'content': 'You are a tutor. Use the pose_question tool to ask the student a question. Always call the tool.'},
        {'role': 'user', 'content': "Let's start the lesson on fractions."},
    ],
    'tools': TOOLS,
    'stream': False,
    'options': {'temperature': 0.2},
}

try:
    r = requests.post('http://localhost:11434/api/chat', json=payload, timeout=180)
    r.raise_for_status()
    data = r.json()
    msg = data.get('message', {})
    tool_calls = msg.get('tool_calls') or []
    print(f"MODEL: {MODEL}")
    print(f"tool_calls returned: {len(tool_calls)}")
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get('function', {})
            print(f"  -> {fn.get('name')}({json.dumps(fn.get('arguments'))[:120]})")
        print("RESULT: TOOL-USE SUPPORTED ✓")
    else:
        print(f"  no tool_calls. content: {(msg.get('content') or '')[:160]!r}")
        print("RESULT: model did NOT tool-call ✗ (simple_tutor would fall back)")
except requests.HTTPError as e:
    print(f"MODEL: {MODEL}")
    print(f"HTTP error: {e} — body: {e.response.text[:200]}")
    print("RESULT: tools likely UNSUPPORTED by this model ✗")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")
