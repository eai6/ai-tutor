"""Build a single self-contained offline_eval/experiment_review/viewer.html.

Embeds every multi-turn session (transcript + judge rubric) inline and renders a
browse table, a session detail view, and a cross-cycle compare (same seed-5
scenario across baseline -> cycle1 -> cycle2). No external resources — open the
file directly in a browser. Re-run any time (picks up new cycles).

    ./venv/bin/python offline_eval/build_viewer.py
"""
import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "offline_eval", "multi_turn_results", "fixcheck")
OUT = os.path.join(ROOT, "offline_eval", "viewer_deploy", "index.html")

RUNS = [
    ("_prefix_colab", "00_prefix_colab"),
    ("_n5_sample5", "01_n5_sample5"),
    ("_stiff_rule", "01b_stiff_gemini"),
    ("_errored_overload", "02_errored_overload_INVALID"),
    ("_cycle0_baseline", "10_cycle0_baseline"),
    ("_cycle1", "11_cycle1_retry+budget"),
    ("_cycle2", "12_cycle2_persona_budgets"),
    ("_cycle3_3model", "13_cycle3_antidesync"),
    ("_cycle4", "14_cycle4_intentgate"),
    ("_cycle5", "15_cycle5_prompt_tuning"),
]


def collect():
    out = []
    for sub, label in RUNS:
        for jf in sorted(glob.glob(os.path.join(SRC, sub, "*.json"))):
            model = os.path.basename(jf)[:-5]
            try:
                data = json.load(open(jf))
            except Exception:
                continue
            for r in data.get("results", []):
                rr = r.get("rubric_result") or {}
                out.append({
                    "run": label, "m": model, "s": r.get("scenario_id"),
                    "p": r.get("persona"), "sub": r.get("subject"),
                    "l": r.get("lesson_id"), "o": r.get("sim_reason"),
                    "pass": bool(r.get("passed")), "t": r.get("sim_turns"),
                    "rm": rr.get("mean_score"), "th": rr.get("pass_threshold"),
                    "e": bool(r.get("error")),
                    "err": str(r.get("error") or "")[:400],
                    "tr": [{"role": t.get("role"), "ph": t.get("phase"),
                            "c": str(t.get("content") or "")}
                           for t in (r.get("transcript") or [])],
                    "ru": [{"it": it.get("item"), "sc": it.get("score"),
                            "ap": bool(it.get("applicable")),
                            "rz": str(it.get("reasoning") or "")}
                           for it in (rr.get("items") or [])],
                })
    return out


TEMPLATE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Multi-turn tutoring — experiment data</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#e2e2e2;--card:#f7f7f8;--accent:#1f3a5f;
 --tutor:#e8f0fe;--stud:#eef7ee;--hi:#fff6e0;--pass:#1a7f37;--fail:#b42318;}
@media(prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e6e6e6;--mut:#9aa0a6;--line:#2c2f36;
 --card:#1e2126;--accent:#8fb8e6;--tutor:#1b2b45;--stud:#16301f;--hi:#3a3320;--pass:#4ac26b;--fail:#f0776c;}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
 background:var(--bg);color:var(--fg)}
header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center;
 position:sticky;top:0;background:var(--bg);z-index:5;flex-wrap:wrap}
h1{font-size:16px;margin:0;color:var(--accent)}
.tab{padding:5px 12px;border:1px solid var(--line);border-radius:6px;cursor:pointer;background:var(--card)}
.tab.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.wrap{padding:12px 16px;max-width:100%}
.filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
select,input{padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
input[type=search]{min-width:240px}
.stat{color:var(--mut);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line);white-space:nowrap}
th{position:sticky;top:57px;background:var(--bg);cursor:pointer;user-select:none}
tbody tr{cursor:pointer}tbody tr:hover{background:var(--card)}
.badge{padding:1px 7px;border-radius:20px;font-size:12px;font-weight:600}
.b-pass{background:var(--stud);color:var(--pass)}.b-fail{background:var(--hi);color:var(--fail)}
.o-exit_ticket{color:var(--pass)}.o-max_turns{color:#b8860b}.o-deadlock,.o-error{color:var(--fail)}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;z-index:20}
.panel{position:fixed;top:0;right:0;height:100%;width:min(820px,96vw);background:var(--bg);
 border-left:1px solid var(--line);overflow:auto;padding:16px;z-index:21}
.close{float:right;cursor:pointer;font-size:20px;color:var(--mut)}
.meta{color:var(--mut);font-size:13px;margin:2px 0 12px}
.sec{font-weight:700;margin:16px 0 8px;color:var(--accent)}
.rub{width:100%;font-size:12.5px}.rub td{vertical-align:top;padding:5px 6px}
.bar{height:8px;border-radius:4px;background:var(--line);width:60px;display:inline-block;vertical-align:middle;overflow:hidden}
.bar>i{display:block;height:100%}
.msg{margin:8px 0;padding:8px 11px;border-radius:8px;max-width:88%}
.msg.tutor{background:var(--tutor)}.msg.student{background:var(--stud);margin-left:auto}
.who{font-size:11px;color:var(--mut);margin-bottom:2px}
.cmp{display:flex;gap:12px;overflow-x:auto}
.col{flex:1 0 380px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--card)}
.small{font-size:12px;color:var(--mut)} pre{white-space:pre-wrap;margin:0;font:inherit}
.about{max-width:940px}.about p{margin:6px 0}.about li{margin:3px 0}
.about h2{color:var(--accent);font-size:15px;margin:22px 0 6px;border-bottom:1px solid var(--line);padding-bottom:4px}
.about td,.about th{white-space:normal;vertical-align:top}.about th{position:static}
.about td:first-child{white-space:nowrap;font-weight:600}
.about code{background:var(--card);padding:1px 5px;border-radius:4px;font-size:12.5px}
</style></head><body>
<header>
 <h1>Multi-turn tutoring — experiment data</h1>
 <span class="tab on" data-v="about" onclick="setView('about')">About &amp; how to use</span>
 <span class="tab" data-v="browse" onclick="setView('browse')">Browse</span>
 <span class="tab" data-v="compare" onclick="setView('compare')">Cross-cycle compare</span>
 <span class="stat" id="count"></span>
</header>

<div class="wrap about" id="about">
 <h2>What this is</h2>
 <p>A browser for the raw data behind our <b>multi-turn tutoring evaluation</b>. Each row is one complete simulated tutoring <b>session</b>: a model under test (the <b>tutor</b>) teaches a simulated <b>student</b> through a whole lesson, and a separate <b>judge</b> scores the finished transcript against a rubric. This page lets you read every transcript and every judge score without opening the raw JSON.</p>
 <p class="small">Generated from the result JSONs by <code>offline_eval/build_viewer.py</code> — re-run it to refresh. It's a static file; nothing here calls out to the network.</p>

 <h2>How a session is scored</h2>
 <ul>
  <li><b>Tutor</b> — the model being evaluated (glm-4.7, gemini-2.5-flash, deepseek-v3.1, kimi-k2-thinking, qwen3-next-80b-instruct). This is the only thing that changes between rows of the same scenario.</li>
  <li><b>Student</b> — a simulated learner (Anthropic Haiku) role-playing one of six <b>personas</b>.</li>
  <li><b>Judge</b> — Anthropic Sonnet at temperature 0, scoring the finished transcript on ~11 rubric items, each 0.00–1.00.</li>
  <li>A session shows <span class="badge b-pass">PASS</span> only when it BOTH reaches the exit ticket AND the rubric <b>mean ≥ threshold</b> (0.6). Reaching the exit ticket alone is not enough.</li>
 </ul>

 <h2>The three tabs</h2>
 <table><tbody>
  <tr><td>About</td><td>this page.</td></tr>
  <tr><td>Browse</td><td>the full session table. Filter with the dropdowns, sort by clicking a column header, and click any row to open its full transcript + judge scores in a panel on the right.</td></tr>
  <tr><td>Cross-cycle compare</td><td>pick one scenario + one model and see that SAME session side-by-side across every run that ran it — the fastest way to check whether a fix actually changed a specific session's behaviour.</td></tr>
 </tbody></table>

 <h2>The filter dropdowns (Browse tab)</h2>
 <table><tbody>
  <tr><td>run</td><td>which round of the experiment (see the run list below).</td></tr>
  <tr><td>model</td><td>the tutor model under test.</td></tr>
  <tr><td>persona</td><td>the simulated student's behaviour (see personas below).</td></tr>
  <tr><td>subject</td><td>math or geography.</td></tr>
  <tr><td>outcome</td><td>how the session ended (see outcome codes below).</td></tr>
  <tr><td>pass</td><td>filter to PASS or FAIL only.</td></tr>
  <tr><td>search box</td><td>free text — matches the scenario id, the transcript content, AND the judge's reasoning. e.g. type <code>contradict</code> to find every session the judge flagged for self-contradiction.</td></tr>
 </tbody></table>
 <p class="small">The counter beside the tabs shows how many sessions match the current filter and their pass-rate. Click a column header to sort; click again to reverse.</p>

 <h2>Student personas</h2>
 <table><tbody>
  <tr><td>struggler</td><td>weak; many wrong answers, needs heavy scaffolding.</td></tr>
  <tr><td>average</td><td>middling; some slips.</td></tr>
  <tr><td>capable</td><td>strong; usually correct.</td></tr>
  <tr><td>probe_resistant</td><td>resists "explain your reasoning" prompts.</td></tr>
  <tr><td>non_responder</td><td>disengages — minimal, off-topic, or refusing answers.</td></tr>
  <tr><td>error_prone</td><td>careless arithmetic / procedural mistakes.</td></tr>
 </tbody></table>

 <h2>Outcome codes</h2>
 <table><tbody>
  <tr><td class="o-exit_ticket">exit_ticket</td><td>reached the graded quiz — the lesson completed. This is the success outcome.</td></tr>
  <tr><td class="o-max_turns">max_turns</td><td>ran out of the turn budget before finishing — a timeout.</td></tr>
  <tr><td class="o-deadlock">deadlock</td><td>tutor and student got stuck in a loop; the engine stopped it.</td></tr>
  <tr><td class="o-error">error</td><td>an infrastructure failure (an API error mid-session), NOT the model's fault — treat as invalid.</td></tr>
 </tbody></table>

 <h2>The runs (chronological)</h2>
 <p>The experiment iterates: implement a fix → run the same 20 scenarios → analyze → fix again. Each "run" is one of those rounds. The later runs (10 / 11 / 12) share the same 20 seed-5 scenarios, which is what makes the compare tab meaningful.</p>
 <table><tbody>
  <tr><td>00_prefix_colab</td><td>earliest baseline, before the anti-repetition &amp; judge fixes. n=5.</td></tr>
  <tr><td>01_n5_sample5</td><td>n=5 smoke test of the first fixes (anti-repetition, end-reason judge note, softened Gemini rule).</td></tr>
  <tr><td>01b_stiff_gemini</td><td>Gemini-only variant testing a stricter (later softened) prompt rule.</td></tr>
  <tr><td>02_errored_overload_INVALID</td><td>an n=20 attempt wiped by an Anthropic API outage. INVALID — kept only as evidence.</td></tr>
  <tr><td>10_cycle0_baseline</td><td>first proper n=20 baseline (seed-5 scenarios).</td></tr>
  <tr><td>11_cycle1_retry+budget</td><td>added transient-error retry (fixes 429/503 deadlocks) + a first turn-budget raise.</td></tr>
  <tr><td>12_cycle2_persona_budgets</td><td>added persona-aware turn budgets. Appears here once that run finishes.</td></tr>
 </tbody></table>

 <h2>Reading a session</h2>
 <ul>
  <li><b>Rubric scores are colour-coded:</b> <span style="color:var(--pass)">green ≥ 0.70</span>, <span style="color:#b8860b">amber 0.40–0.69</span>, <span style="color:var(--fail)">red &lt; 0.40</span>. An item marked <b>n/a</b> did not apply to that session and is excluded from the mean.</li>
  <li><b>Transcript:</b> blue bubbles = tutor, green = student. The <i>phase</i> label on tutor turns (engage / explore / explain / elaborate / evaluate) is the 5E teaching phase for that lesson step.</li>
 </ul>

 <h2>Caveats — read before drawing conclusions</h2>
 <ul>
  <li><b>Small samples are noisy.</b> The n=5 runs have a ±~22-point margin — treat them as directional only. The n=20 cycles (±~11pp) are the trustworthy comparison.</li>
  <li><b>Some scenarios are meant to be hard.</b> <code>speedrun_*</code> and <code>short_session_*</code> have deliberately tight turn budgets; timing out on those is by design, not a tutor failure.</li>
  <li><b>02_errored_overload is not real data</b> — an API outage failed every session in it. It's here so the failure is visible, not to be scored.</li>
 </ul>
</div>

<div class="wrap" id="browse" style="display:none">
 <div class="filters">
  <select id="f-run" onchange="draw()"></select>
  <select id="f-model" onchange="draw()"></select>
  <select id="f-persona" onchange="draw()"></select>
  <select id="f-subject" onchange="draw()"></select>
  <select id="f-outcome" onchange="draw()"></select>
  <select id="f-pass" onchange="draw()"><option value="">pass: any</option><option value="1">PASS</option><option value="0">FAIL</option></select>
  <input type="search" id="f-text" placeholder="search scenario / transcript / judge reasoning…" oninput="draw()">
 </div>
 <table><thead><tr>
  <th onclick="sortBy('run')">run</th><th onclick="sortBy('m')">model</th>
  <th onclick="sortBy('s')">scenario</th><th onclick="sortBy('p')">persona</th>
  <th onclick="sortBy('sub')">subj</th><th onclick="sortBy('o')">outcome</th>
  <th onclick="sortBy('pass')">result</th><th onclick="sortBy('t')">turns</th>
  <th onclick="sortBy('rm')">rubric</th>
 </tr></thead><tbody id="rows"></tbody></table>
</div>

<div class="wrap" id="compare" style="display:none">
 <div class="filters">
  <select id="c-scenario" onchange="drawCompare()"></select>
  <select id="c-model" onchange="drawCompare()"></select>
  <span class="small">Shows the SAME scenario for one model across every run that ran it — see whether a cycle's fix changed that session.</span>
 </div>
 <div class="cmp" id="cmpcols"></div>
</div>

<div class="overlay" id="ov" onclick="if(event.target.id=='ov')closeP()"></div>
<div class="panel" id="panel" style="display:none"></div>

<script>
const DATA = __DATA__;
let sortKey='run', sortDir=1;

function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function uniq(k){return [...new Set(DATA.map(d=>d[k]).filter(x=>x!=null))].sort();}
function fillSel(id,label,vals){const s=document.getElementById(id);s.innerHTML='<option value="">'+label+': all</option>'+vals.map(v=>'<option>'+esc(v)+'</option>').join('');}
function scoreColor(v){if(v==null)return 'var(--mut)';return v>=0.7?'var(--pass)':v>=0.4?'#b8860b':'var(--fail)';}

function transcriptHTML(tr){return tr.map(t=>{
 const cls=t.role==='tutor'?'tutor':'student';
 const ph=t.ph&&t.role==='tutor'?' · '+esc(t.ph):'';
 return '<div class="msg '+cls+'"><div class="who">'+(t.role==='tutor'?'TUTOR':'student')+ph+'</div><pre>'+esc(t.c)+'</pre></div>';
}).join('');}

function rubricHTML(ru){if(!ru.length)return '<div class="small">no rubric scored (session errored or ended before judging).</div>';
 return '<table class="rub"><tbody>'+ru.map(it=>{
  const v=it.sc, w=(v==null?0:Math.max(0,Math.min(1,v))*100);
  return '<tr><td><span class="bar"><i style="width:'+w+'%;background:'+scoreColor(v)+'"></i></span> '+
   '<b style="color:'+scoreColor(v)+'">'+(v==null?'n/a':v.toFixed(2))+'</b>'+(it.ap?'':' <span class="small">(n/a)</span>')+
   '</td><td><b>'+esc(it.it)+'</b><br><span class="small">'+esc(it.rz)+'</span></td></tr>';
 }).join('')+'</tbody></table>';}

function sessionHTML(d){
 const rm=d.rm==null?'—':d.rm.toFixed(2);
 return '<div class="meta">'+esc(d.run)+' · <b>'+esc(d.m)+'</b> · persona '+esc(d.p)+' · '+esc(d.sub)+' · lesson '+esc(d.l)+
  '</div><div>outcome <b class="o-'+esc(d.o)+'">'+esc(d.o)+'</b> · '+
  '<span class="badge '+(d.pass?'b-pass':'b-fail')+'">'+(d.pass?'PASS':'FAIL')+'</span> · turns '+esc(d.t)+
  ' · rubric mean <b>'+rm+'</b> / '+esc(d.th)+'</div>'+
  (d.e?'<div class="small" style="color:var(--fail)">ERROR: '+esc(d.err)+'</div>':'')+
  '<div class="sec">Judge (rubric)</div>'+rubricHTML(d.ru)+
  '<div class="sec">Transcript</div>'+transcriptHTML(d.tr);
}

function openP(i){const d=SHOWN[i];document.getElementById('panel').innerHTML='<span class="close" onclick="closeP()">×</span><h2 style="margin:.2em 0">'+esc(d.s)+'</h2>'+sessionHTML(d);
 document.getElementById('ov').style.display='block';document.getElementById('panel').style.display='block';}
function closeP(){document.getElementById('ov').style.display='none';document.getElementById('panel').style.display='none';}

let SHOWN=[];
function draw(){
 const fr=v('f-run'),fm=v('f-model'),fp=v('f-persona'),fs=v('f-subject'),fo=v('f-outcome'),fpass=v('f-pass'),ft=v('f-text').toLowerCase();
 SHOWN=DATA.filter(d=>(!fr||d.run===fr)&&(!fm||d.m===fm)&&(!fp||d.p===fp)&&(!fs||d.sub===fs)&&(!fo||d.o===fo)&&
  (fpass===''||String(d.pass?1:0)===fpass)&&
  (!ft||d.s.toLowerCase().includes(ft)||d.tr.some(t=>t.c.toLowerCase().includes(ft))||d.ru.some(r=>(r.rz||'').toLowerCase().includes(ft))));
 SHOWN.sort((a,b)=>{let x=a[sortKey],y=b[sortKey];if(x==null)x='';if(y==null)y='';return (x>y?1:x<y?-1:0)*sortDir;});
 document.getElementById('rows').innerHTML=SHOWN.map((d,i)=>'<tr onclick="openP('+i+')">'+
  '<td class="small">'+esc(d.run)+'</td><td>'+esc(d.m)+'</td><td>'+esc(d.s)+'</td><td>'+esc(d.p)+'</td>'+
  '<td>'+esc(d.sub)+'</td><td class="o-'+esc(d.o)+'">'+esc(d.o)+'</td>'+
  '<td><span class="badge '+(d.pass?'b-pass':'b-fail')+'">'+(d.pass?'PASS':'FAIL')+'</span></td>'+
  '<td>'+esc(d.t)+'</td><td>'+(d.rm==null?'—':d.rm.toFixed(2))+'</td></tr>').join('');
 const np=SHOWN.filter(d=>d.pass).length;
 document.getElementById('count').textContent=SHOWN.length+' sessions · '+np+' pass ('+(SHOWN.length?Math.round(100*np/SHOWN.length):0)+'%)';
}
function v(id){return document.getElementById(id).value;}
function sortBy(k){if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=1;}draw();}

function setView(view){
 document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.v===view));
 document.getElementById('about').style.display=view==='about'?'':'none';
 document.getElementById('browse').style.display=view==='browse'?'':'none';
 document.getElementById('compare').style.display=view==='compare'?'':'none';
 if(view==='compare')drawCompare();
}
function drawCompare(){
 const sc=v('c-scenario'),m=v('c-model');
 const set=DATA.filter(d=>d.s===sc&&d.m===m);
 // order columns by run label
 set.sort((a,b)=>a.run>b.run?1:-1);
 document.getElementById('cmpcols').innerHTML=set.length?set.map(d=>'<div class="col">'+
  '<div class="small">'+esc(d.run)+'</div><h3 style="margin:.2em 0">'+
  '<span class="badge '+(d.pass?'b-pass':'b-fail')+'">'+(d.pass?'PASS':'FAIL')+'</span> '+
  '<span class="o-'+esc(d.o)+'">'+esc(d.o)+'</span></h3>'+
  '<div class="small">rubric '+(d.rm==null?'—':d.rm.toFixed(2))+' · '+esc(d.t)+' turns</div>'+
  rubricHTML(d.ru)+'<div class="sec">Transcript</div>'+transcriptHTML(d.tr)+'</div>'
 ).join(''):'<div class="small">This model didn\'t run this scenario.</div>';
}

// init
fillSel('f-run','run',uniq('run'));fillSel('f-model','model',uniq('m'));
fillSel('f-persona','persona',uniq('p'));fillSel('f-subject','subject',uniq('sub'));
fillSel('f-outcome','outcome',uniq('o'));
document.getElementById('c-scenario').innerHTML=uniq('s').map(x=>'<option>'+esc(x)+'</option>').join('');
document.getElementById('c-model').innerHTML=uniq('m').map(x=>'<option>'+esc(x)+'</option>').join('');
draw();
</script></body></html>"""


def build():
    sessions = collect()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    html = TEMPLATE.replace("__DATA__", json.dumps(sessions, ensure_ascii=False).replace("</", "<\\/"))
    with open(OUT, "w") as fh:
        fh.write(html)
    mb = os.path.getsize(OUT) / 1e6
    print(f"wrote {OUT}  ({len(sessions)} sessions, {mb:.1f} MB)")


if __name__ == "__main__":
    build()
