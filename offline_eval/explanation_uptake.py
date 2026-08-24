"""Did the tutor actually USE the question bank's explanation when praising a
correct answer?

    python offline_eval/explanation_uptake.py offline_eval/multi_turn_results/*/trace/*.jsonl

The engine sends `<explanation>` on every CORRECT verdict and instructs the
tutor to "open with the REASON it is right". Sending it is not using it. This
measures the second thing.

WHY NOT WORD OVERLAP. The obvious metric — content words shared between the
explanation and the reply — under-counts math badly. A bank explanation reads
"Probability = 4 / (4 + 6) = 0.4"; a good reply reads "the number of green
sections (4) divided by the total sections (10), which gives you 0.4". That
reply is a textbook restatement and shares ONE word. Word overlap scored it a
miss. Numbers and operators carry the meaning in math, so they are counted too.

WHY ONLY THE ACKNOWLEDGEMENT SPAN. The reply usually ends with the NEXT
question, which brings its own numbers and vocabulary. Scoring the whole reply
lets that spill in and inflates uptake. Only the text before the next question
is the acknowledgement, and that is what the instruction governs.
"""
import json
import re
import sys

STOP = set("""the a an is are was were of to in on for and or it its that this with as by
be been at from which what how why you your they them their there here so if then than
into not no yes we our can will would answer correct right correct's option""".split())

# The next question starts at a blank line or an "A)" style option block. The
# option marker must be matched ANYWHERE, not just at line start: models often
# emit the whole question inline ("Which is true? A) ... B) ..."), and anchoring
# to \n let that entire question count as acknowledgement text — which scored a
# leaked question as an explanation.
_Q_START = re.compile(r"\n\s*\n|\bA\)|\?\s*$", re.M)

_PRAISE = re.compile(
    r"\b(good|great|nice|well done|exactly|correct|yes|perfect|spot on|catch|work|job|"
    r"right|got it|excellent|that's it|bingo)\b", re.I)
_CONNECTIVE = re.compile(
    r"\b(because|since|divid|multipl|subtract|add(ing|s)?|means|equals|gives|"
    r"comes from|which is|so the|that is why|ratio|out of|per cent|percent)\b", re.I)


def ack_span(reply: str) -> str:
    """The acknowledgement, i.e. everything before the next posed question."""
    m = _Q_START.search(reply)
    return (reply[:m.start()] if m else reply).strip()


def nums(t: str) -> set:
    # Normalise so "0.40" and "0.4" match, and drop bare option letters.
    out = set()
    for n in re.findall(r"\d+(?:\.\d+)?", t):
        out.add(str(float(n)))
    return out


def words(t: str) -> set:
    return {w for w in re.findall(r"[a-z]{4,}", t.lower())} - STOP


def scan(path: str) -> dict:
    correct = sent = used = praise_only = 0
    misses, hits = [], []
    for line in open(path):
        r = json.loads(line)
        if r.get("verdict") != "correct":
            continue
        correct += 1
        blob = next((s.get("sent", "") for s in (r.get("call2_sent") or [])
                     if s.get("tool") == "record_answer"), "")
        m = re.search(r"<explanation>(.*?)</explanation>", blob, re.S)
        if not m:
            continue
        sent += 1
        expl, ack = m.group(1).strip(), ack_span(r.get("reply", ""))
        hit = bool(nums(expl) & nums(ack)) or len(words(expl) & words(ack)) >= 1 \
            or bool(_CONNECTIVE.search(ack))
        used += hit
        if not hit and _PRAISE.search(ack) and len(ack.split()) <= 8:
            praise_only += 1
        (hits if hit else misses).append((expl, ack))
    return {"correct": correct, "sent": sent, "used": used,
            "praise_only": praise_only, "misses": misses, "hits": hits}


def main() -> int:
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        return 2
    print(f"{'board / model':<44}{'correct':>8}{'sent':>7}{'used':>7}{'uptake':>9}{'praise-only':>13}")
    print("-" * 88)
    keep = None
    for p in paths:
        s = scan(p)
        keep = keep or s
        parts = p.split("/")
        label = f"{parts[-3]}/{parts[-1].replace('.jsonl','')}"[:43]
        if not s["sent"]:
            print(f"{label:<44}{s['correct']:>8}{0:>7}{'':>7}{'  no <explanation> sent':>9}")
            continue
        print(f"{label:<44}{s['correct']:>8}{s['sent']:>7}{s['used']:>7}"
              f"{100*s['used']/s['sent']:>8.0f}%{s['praise_only']:>13}")

    if len(paths) == 1 and keep:
        for tag, rows in (("USED", keep["hits"]), ("MISSED", keep["misses"])):
            print(f"\n--- {tag} samples ---")
            for expl, ack in rows[:4]:
                print(f"  expl: {expl[:110]}")
                print(f"  ack : {ack[:110]}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
