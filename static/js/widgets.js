/**
 * Interactive widget registry for lesson content.
 *
 * Backend ships declarative specs (see apps/curriculum/widgets.py); this file
 * turns a spec into live DOM. Loaded in the student chat and in the teacher
 * step-edit preview so both render identically.
 *
 *   AfricaTutorWidgets.render(container, mediaItem)
 *     container  — empty DOM node to mount into
 *     mediaItem  — { type: 'widget', widget_type, title, caption, alt, params }
 *
 * Each widget_type has its own render function registered in WIDGETS below.
 * All state is component-local; widgets don't share globals.
 *
 * Expression evaluator (for composite_index_explorer and function_plotter) is
 * a small AST walker — no eval/Function. Only arithmetic, comparisons, and a
 * whitelist of math functions are accepted. The Python side validates at
 * authoring time; this is the runtime twin.
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // Safe expression evaluator
  // ---------------------------------------------------------------------------

  const ALLOWED_FUNCS = {
    abs: Math.abs, min: Math.min, max: Math.max, round: Math.round,
    sqrt: Math.sqrt, log: Math.log, log10: Math.log10, exp: Math.exp,
    sin: Math.sin, cos: Math.cos, tan: Math.tan,
    asin: Math.asin, acos: Math.acos, atan: Math.atan, pow: Math.pow,
  };
  const ALLOWED_CONSTS = { pi: Math.PI, e: Math.E };

  // Tiny tokenizer + Pratt parser. Understands: numbers, identifiers, + - * / %
  // ** (power), parentheses, function calls, unary +/-.
  function tokenize(expr) {
    const tokens = [];
    let i = 0;
    while (i < expr.length) {
      const c = expr[i];
      if (/\s/.test(c)) { i++; continue; }
      if (/[0-9.]/.test(c)) {
        let j = i;
        while (j < expr.length && /[0-9.]/.test(expr[j])) j++;
        tokens.push({ type: 'num', value: parseFloat(expr.slice(i, j)) });
        i = j; continue;
      }
      if (/[a-zA-Z_]/.test(c)) {
        let j = i;
        while (j < expr.length && /[a-zA-Z0-9_]/.test(expr[j])) j++;
        tokens.push({ type: 'name', value: expr.slice(i, j) });
        i = j; continue;
      }
      if (c === '*' && expr[i + 1] === '*') { tokens.push({ type: 'op', value: '**' }); i += 2; continue; }
      if ('+-*/%()^,'.includes(c)) { tokens.push({ type: 'op', value: c === '^' ? '**' : c }); i++; continue; }
      throw new Error('unexpected char: ' + c);
    }
    return tokens;
  }

  function parseExpression(tokens) {
    let pos = 0;
    function peek() { return tokens[pos]; }
    function consume(type, value) {
      const t = tokens[pos];
      if (!t || t.type !== type || (value !== undefined && t.value !== value)) {
        throw new Error('parse error at ' + pos);
      }
      pos++;
      return t;
    }
    // expr := add ; add := mul (('+'|'-') mul)* ; mul := pow (('*'|'/'|'%') pow)*
    // pow := unary ('**' pow)? ; unary := ('+'|'-') unary | atom
    function parseAtom() {
      const t = tokens[pos];
      if (!t) throw new Error('unexpected end');
      if (t.type === 'num') { pos++; return { kind: 'num', value: t.value }; }
      if (t.type === 'name') {
        pos++;
        if (peek() && peek().type === 'op' && peek().value === '(') {
          pos++;
          const args = [];
          if (!(peek() && peek().type === 'op' && peek().value === ')')) {
            args.push(parseAdd());
            while (peek() && peek().type === 'op' && peek().value === ',') {
              pos++;
              args.push(parseAdd());
            }
          }
          consume('op', ')');
          return { kind: 'call', name: t.value, args };
        }
        return { kind: 'name', value: t.value };
      }
      if (t.type === 'op' && t.value === '(') {
        pos++;
        const e = parseAdd();
        consume('op', ')');
        return e;
      }
      throw new Error('unexpected token: ' + JSON.stringify(t));
    }
    function parseUnary() {
      const t = peek();
      if (t && t.type === 'op' && (t.value === '+' || t.value === '-')) {
        pos++;
        const operand = parseUnary();
        return { kind: 'unary', op: t.value, operand };
      }
      return parseAtom();
    }
    function parsePow() {
      const left = parseUnary();
      const t = peek();
      if (t && t.type === 'op' && t.value === '**') {
        pos++;
        const right = parsePow(); // right-associative
        return { kind: 'binop', op: '**', left, right };
      }
      return left;
    }
    function parseMul() {
      let left = parsePow();
      while (peek() && peek().type === 'op' && ['*', '/', '%'].includes(peek().value)) {
        const op = tokens[pos++].value;
        const right = parsePow();
        left = { kind: 'binop', op, left, right };
      }
      return left;
    }
    function parseAdd() {
      let left = parseMul();
      while (peek() && peek().type === 'op' && ['+', '-'].includes(peek().value)) {
        const op = tokens[pos++].value;
        const right = parseMul();
        left = { kind: 'binop', op, left, right };
      }
      return left;
    }
    const root = parseAdd();
    if (pos < tokens.length) throw new Error('trailing input');
    return root;
  }

  function evalNode(node, env) {
    switch (node.kind) {
      case 'num': return node.value;
      case 'name':
        if (node.value in env) return env[node.value];
        if (node.value in ALLOWED_CONSTS) return ALLOWED_CONSTS[node.value];
        throw new Error('unknown name: ' + node.value);
      case 'unary': {
        const v = evalNode(node.operand, env);
        return node.op === '-' ? -v : +v;
      }
      case 'binop': {
        const l = evalNode(node.left, env), r = evalNode(node.right, env);
        switch (node.op) {
          case '+': return l + r;
          case '-': return l - r;
          case '*': return l * r;
          case '/': return l / r;
          case '%': return l % r;
          case '**': return Math.pow(l, r);
        }
        throw new Error('unknown op: ' + node.op);
      }
      case 'call': {
        const fn = ALLOWED_FUNCS[node.name];
        if (!fn) throw new Error('function not allowed: ' + node.name);
        return fn.apply(null, node.args.map(a => evalNode(a, env)));
      }
    }
    throw new Error('unknown node: ' + node.kind);
  }

  function compileExpression(expr) {
    const ast = parseExpression(tokenize(expr));
    return function (env) { return evalNode(ast, env); };
  }

  // ---------------------------------------------------------------------------
  // Shared primitives
  // ---------------------------------------------------------------------------

  function el(tag, attrs, children) {
    const n = document.createElementNS(
      tag === 'svg' || tag === 'g' || tag === 'path' || tag === 'circle' ||
      tag === 'rect' || tag === 'line' || tag === 'text' || tag === 'polyline'
        ? 'http://www.w3.org/2000/svg'
        : 'http://www.w3.org/1999/xhtml',
      tag
    );
    if (attrs) for (const k in attrs) {
      if (k === 'style') n.setAttribute('style', attrs[k]);
      else if (k.startsWith('on')) n.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
      else n.setAttribute(k, attrs[k]);
    }
    if (children) for (const c of [].concat(children)) {
      if (c == null) continue;
      n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return n;
  }

  function header(mediaItem) {
    const wrap = document.createElement('div');
    wrap.className = 'widget-header';
    wrap.style.cssText = 'margin-bottom:10px';
    const h = document.createElement('div');
    h.style.cssText = 'font-weight:600;font-size:0.95rem;color:#18181b;margin-bottom:2px';
    h.textContent = mediaItem.title || mediaItem.caption || 'Interactive';
    wrap.appendChild(h);
    if (mediaItem.caption && mediaItem.caption !== mediaItem.title) {
      const c = document.createElement('div');
      c.style.cssText = 'font-size:0.8rem;color:#71717a';
      c.textContent = mediaItem.caption;
      wrap.appendChild(c);
    }
    return wrap;
  }

  function sliderRow(cfg, onChange) {
    const row = document.createElement('div');
    row.style.cssText = 'margin:8px 0;font-size:0.85rem';

    const top = document.createElement('div');
    top.style.cssText = 'display:flex;justify-content:space-between;align-items:baseline;gap:8px';

    const label = document.createElement('label');
    label.textContent = cfg.label;
    label.style.cssText = 'color:#52525b';
    label.htmlFor = cfg.id;

    const valueEl = document.createElement('span');
    valueEl.style.cssText = 'font-variant-numeric:tabular-nums;color:#18181b;font-weight:500';

    const formatValue = (v) => {
      const precision = cfg.step < 1 ? 2 : 0;
      const n = Number(v).toFixed(precision);
      return cfg.unit ? `${n} ${cfg.unit}` : n;
    };

    const input = document.createElement('input');
    input.type = 'range';
    input.id = cfg.id;
    input.min = cfg.min;
    input.max = cfg.max;
    input.step = cfg.step;
    input.value = cfg.default;
    input.style.cssText = 'width:100%;margin-top:4px';
    input.setAttribute('aria-label', cfg.label);

    valueEl.textContent = formatValue(input.value);
    input.addEventListener('input', () => {
      valueEl.textContent = formatValue(input.value);
      onChange(parseFloat(input.value));
    });

    top.appendChild(label);
    top.appendChild(valueEl);
    row.appendChild(top);
    row.appendChild(input);
    return { row, input };
  }

  // ---------------------------------------------------------------------------
  // composite_index_explorer
  // ---------------------------------------------------------------------------

  function renderCompositeIndex(container, item) {
    const params = item.params || {};
    const inputs = (params.inputs || []);
    const bands = (params.bands || []).slice().sort((a, b) => a.min - b.min);
    const outputMin = params.output_min != null ? params.output_min : 0;
    const outputMax = params.output_max != null ? params.output_max : 1;
    const precision = params.precision != null ? params.precision : 3;
    const references = params.references || [];

    let evaluator;
    try {
      evaluator = compileExpression(params.formula || '0');
    } catch (err) {
      container.textContent = 'Invalid formula in widget spec: ' + err.message;
      return;
    }

    container.innerHTML = '';
    container.appendChild(header(item));

    const state = {};
    const controls = document.createElement('div');
    controls.style.cssText = 'display:flex;flex-direction:column;gap:2px';

    const scoreWrap = document.createElement('div');
    scoreWrap.style.cssText = 'margin-top:14px;padding:12px;background:#f4f4f5;border-radius:8px';
    const scoreLabel = document.createElement('div');
    scoreLabel.style.cssText = 'font-size:0.78rem;color:#71717a;text-transform:uppercase;letter-spacing:0.04em';
    scoreLabel.textContent = params.output_label || 'Score';
    const scoreValue = document.createElement('div');
    scoreValue.style.cssText = 'font-size:1.6rem;font-weight:700;color:#18181b;font-variant-numeric:tabular-nums';
    scoreValue.setAttribute('role', 'status');
    scoreValue.setAttribute('aria-live', 'polite');
    const bandLabel = document.createElement('div');
    bandLabel.style.cssText = 'display:inline-block;margin-top:4px;padding:2px 10px;border-radius:999px;font-size:0.78rem;color:white;font-weight:500';

    const barWrap = document.createElement('div');
    barWrap.style.cssText = 'position:relative;height:12px;margin-top:10px;background:#e4e4e7;border-radius:6px;overflow:visible';
    const barFill = document.createElement('div');
    barFill.style.cssText = 'height:100%;border-radius:6px;transition:width 0.1s linear,background-color 0.1s linear';
    barWrap.appendChild(barFill);

    // Reference markers
    for (const ref of references) {
      const pct = ((ref.value - outputMin) / (outputMax - outputMin)) * 100;
      if (pct < 0 || pct > 100) continue;
      const mark = document.createElement('div');
      mark.style.cssText =
        `position:absolute;top:-3px;bottom:-3px;left:${pct}%;width:2px;background:#18181b`;
      mark.title = `${ref.label}: ${ref.value}`;
      const tag = document.createElement('div');
      tag.textContent = ref.label;
      tag.style.cssText =
        `position:absolute;top:-20px;left:${pct}%;transform:translateX(-50%);` +
        `font-size:0.7rem;color:#52525b;white-space:nowrap`;
      barWrap.appendChild(mark);
      barWrap.appendChild(tag);
    }

    scoreWrap.appendChild(scoreLabel);
    scoreWrap.appendChild(scoreValue);
    scoreWrap.appendChild(bandLabel);
    scoreWrap.appendChild(barWrap);

    function bandFor(score) {
      let current = null;
      for (const b of bands) if (score >= b.min) current = b;
      return current;
    }

    function update() {
      let score;
      try {
        score = evaluator(state);
      } catch (err) {
        scoreValue.textContent = '—';
        bandLabel.textContent = err.message;
        return;
      }
      scoreValue.textContent = Number(score).toFixed(precision);
      const pct = Math.max(0, Math.min(100, ((score - outputMin) / (outputMax - outputMin)) * 100));
      barFill.style.width = pct + '%';
      const band = bandFor(score);
      if (band) {
        bandLabel.textContent = band.label;
        bandLabel.style.background = band.color || '#52525b';
        barFill.style.background = band.color || '#52525b';
      } else {
        bandLabel.textContent = '';
        barFill.style.background = '#52525b';
      }
    }

    inputs.forEach((inp, i) => {
      state[inp.key] = inp.default;
      const { row } = sliderRow({
        id: `widget-${Math.random().toString(36).slice(2)}-slider-${i}`,
        label: inp.label + (inp.unit ? ` (${inp.unit})` : ''),
        min: inp.min, max: inp.max, step: inp.step, default: inp.default,
        unit: '',
      }, (v) => { state[inp.key] = v; update(); });
      controls.appendChild(row);
    });

    container.appendChild(controls);
    container.appendChild(scoreWrap);
    update();
  }

  // ---------------------------------------------------------------------------
  // function_plotter
  // ---------------------------------------------------------------------------

  function renderFunctionPlotter(container, item) {
    const params = item.params || {};
    const xMin = params.x_min != null ? params.x_min : -10;
    const xMax = params.x_max != null ? params.x_max : 10;
    let yMin = params.y_min, yMax = params.y_max;
    const autoY = yMin == null || yMax == null;
    const parameters = params.parameters || [];
    const refs = params.reference_points || [];

    let evaluator;
    try { evaluator = compileExpression(params.expression || 'x'); }
    catch (err) { container.textContent = 'Invalid formula: ' + err.message; return; }

    container.innerHTML = '';
    container.appendChild(header(item));

    const state = {};
    const controls = document.createElement('div');
    controls.style.cssText = 'display:flex;flex-direction:column;gap:2px';
    parameters.forEach((p, i) => {
      state[p.key] = p.default;
      const { row } = sliderRow({
        id: `widget-plot-${Math.random().toString(36).slice(2)}-${i}`,
        label: p.label, min: p.min, max: p.max, step: p.step, default: p.default,
      }, (v) => { state[p.key] = v; draw(); });
      controls.appendChild(row);
    });
    container.appendChild(controls);

    const svgNS = 'http://www.w3.org/2000/svg';
    const W = 320, H = 220, PAD = 28;
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', item.alt || item.title || 'function plot');
    svg.style.cssText = 'width:100%;height:auto;margin-top:12px;background:#fafafa;border-radius:8px;border:1px solid #e4e4e7';
    container.appendChild(svg);

    const readout = document.createElement('div');
    readout.style.cssText = 'font-size:0.78rem;color:#52525b;margin-top:4px;font-variant-numeric:tabular-nums';
    readout.setAttribute('aria-live', 'polite');
    container.appendChild(readout);

    function draw() {
      while (svg.firstChild) svg.removeChild(svg.firstChild);

      const samples = 160;
      const xs = [], ys = [];
      for (let i = 0; i <= samples; i++) {
        const x = xMin + (xMax - xMin) * (i / samples);
        state.x = x;
        let y;
        try { y = evaluator(state); } catch { y = NaN; }
        if (!Number.isFinite(y)) { xs.push(x); ys.push(null); continue; }
        xs.push(x); ys.push(y);
      }
      let yLo = yMin, yHi = yMax;
      if (autoY) {
        const finite = ys.filter(v => v != null && Number.isFinite(v));
        if (finite.length) {
          yLo = Math.min.apply(null, finite);
          yHi = Math.max.apply(null, finite);
          if (yLo === yHi) { yLo -= 1; yHi += 1; }
          const pad = (yHi - yLo) * 0.1;
          yLo -= pad; yHi += pad;
        } else { yLo = -1; yHi = 1; }
      }

      const toPx = (x, y) => [
        PAD + ((x - xMin) / (xMax - xMin)) * (W - 2 * PAD),
        H - PAD - ((y - yLo) / (yHi - yLo)) * (H - 2 * PAD),
      ];

      // Grid + axes
      const axisColor = '#e4e4e7';
      const axisStroke = (a, b) => {
        const line = document.createElementNS(svgNS, 'line');
        line.setAttribute('x1', a[0]); line.setAttribute('y1', a[1]);
        line.setAttribute('x2', b[0]); line.setAttribute('y2', b[1]);
        line.setAttribute('stroke', axisColor); line.setAttribute('stroke-width', 1);
        svg.appendChild(line);
      };
      axisStroke([PAD, H - PAD], [W - PAD, H - PAD]);
      axisStroke([PAD, PAD], [PAD, H - PAD]);
      // x=0 and y=0 if visible
      if (xMin <= 0 && xMax >= 0) {
        const [x0] = toPx(0, 0);
        const line = document.createElementNS(svgNS, 'line');
        line.setAttribute('x1', x0); line.setAttribute('y1', PAD);
        line.setAttribute('x2', x0); line.setAttribute('y2', H - PAD);
        line.setAttribute('stroke', '#a1a1aa'); line.setAttribute('stroke-dasharray', '2 3');
        svg.appendChild(line);
      }
      if (yLo <= 0 && yHi >= 0) {
        const [, y0] = toPx(0, 0);
        const line = document.createElementNS(svgNS, 'line');
        line.setAttribute('x1', PAD); line.setAttribute('y1', y0);
        line.setAttribute('x2', W - PAD); line.setAttribute('y2', y0);
        line.setAttribute('stroke', '#a1a1aa'); line.setAttribute('stroke-dasharray', '2 3');
        svg.appendChild(line);
      }

      // Tick labels
      const tickText = (x, y, s) => {
        const t = document.createElementNS(svgNS, 'text');
        t.setAttribute('x', x); t.setAttribute('y', y);
        t.setAttribute('font-size', 9); t.setAttribute('fill', '#71717a');
        t.setAttribute('text-anchor', 'middle'); t.textContent = s;
        svg.appendChild(t);
      };
      tickText(PAD, H - PAD + 12, String(xMin));
      tickText(W - PAD, H - PAD + 12, String(xMax));
      tickText(PAD - 14, H - PAD, yLo.toFixed(1));
      tickText(PAD - 14, PAD + 4, yHi.toFixed(1));

      // Axis labels
      const axLabel = (x, y, s, anchor) => {
        const t = document.createElementNS(svgNS, 'text');
        t.setAttribute('x', x); t.setAttribute('y', y);
        t.setAttribute('font-size', 10); t.setAttribute('fill', '#52525b');
        t.setAttribute('text-anchor', anchor); t.textContent = s;
        svg.appendChild(t);
      };
      axLabel(W - 4, H - PAD + 12, params.x_label || 'x', 'end');
      axLabel(PAD, PAD - 8, params.y_label || 'y', 'middle');

      // Plot
      let d = '';
      let pen = false;
      for (let i = 0; i < xs.length; i++) {
        const y = ys[i];
        if (y == null || !Number.isFinite(y)) { pen = false; continue; }
        const [px, py] = toPx(xs[i], y);
        if (py < 0 || py > H) { pen = false; continue; }
        d += (pen ? 'L' : 'M') + px.toFixed(2) + ' ' + py.toFixed(2) + ' ';
        pen = true;
      }
      const path = document.createElementNS(svgNS, 'path');
      path.setAttribute('d', d.trim());
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', '#3b82f6');
      path.setAttribute('stroke-width', 2);
      svg.appendChild(path);

      // Reference points
      for (const r of refs) {
        const [px, py] = toPx(r.x, r.y);
        if (px < PAD || px > W - PAD || py < PAD || py > H - PAD) continue;
        const c = document.createElementNS(svgNS, 'circle');
        c.setAttribute('cx', px); c.setAttribute('cy', py); c.setAttribute('r', 3);
        c.setAttribute('fill', '#ef4444');
        svg.appendChild(c);
        if (r.label) {
          const t = document.createElementNS(svgNS, 'text');
          t.setAttribute('x', px + 5); t.setAttribute('y', py - 5);
          t.setAttribute('font-size', 9); t.setAttribute('fill', '#52525b');
          t.textContent = r.label;
          svg.appendChild(t);
        }
      }

      // Readout for midpoint (or x=0 if in range)
      const refX = (xMin <= 0 && xMax >= 0) ? 0 : (xMin + xMax) / 2;
      state.x = refX;
      let refY;
      try { refY = evaluator(state); } catch { refY = NaN; }
      readout.textContent =
        `f(${refX.toFixed(2)}) = ${Number.isFinite(refY) ? refY.toFixed(3) : '—'}`;
    }

    draw();
  }

  // ---------------------------------------------------------------------------
  // fraction_decimal_percent
  // ---------------------------------------------------------------------------

  function renderFractionDecimalPercent(container, item) {
    const params = item.params || {};
    const denom = params.denominator || 10;
    const showBar = params.show_bar !== false;
    const showPie = params.show_pie !== false;
    const showNumberLine = params.show_number_line !== false;

    container.innerHTML = '';
    container.appendChild(header(item));

    const state = { num: Math.min(params.default_numerator || 0, denom) };

    const { row, input } = sliderRow({
      id: `fdp-${Math.random().toString(36).slice(2)}`,
      label: `Numerator (out of ${denom})`,
      min: 0, max: denom, step: 1, default: state.num,
    }, (v) => { state.num = Math.round(v); draw(); });
    container.appendChild(row);

    const readouts = document.createElement('div');
    readouts.style.cssText = 'display:flex;gap:14px;justify-content:space-around;margin:12px 0;font-variant-numeric:tabular-nums';
    readouts.setAttribute('aria-live', 'polite');
    const readoutBlock = (label) => {
      const wrap = document.createElement('div');
      wrap.style.cssText = 'text-align:center';
      const l = document.createElement('div');
      l.style.cssText = 'font-size:0.7rem;color:#71717a;text-transform:uppercase;letter-spacing:0.04em';
      l.textContent = label;
      const v = document.createElement('div');
      v.style.cssText = 'font-size:1.1rem;font-weight:600;color:#18181b;margin-top:2px';
      wrap.appendChild(l);
      wrap.appendChild(v);
      return { wrap, v };
    };
    const fracOut = readoutBlock('Fraction');
    const decOut = readoutBlock('Decimal');
    const pctOut = readoutBlock('Percent');
    readouts.appendChild(fracOut.wrap);
    readouts.appendChild(decOut.wrap);
    readouts.appendChild(pctOut.wrap);
    container.appendChild(readouts);

    // Visual row: bar + pie side-by-side, number line below
    const visuals = document.createElement('div');
    visuals.style.cssText = 'display:flex;gap:16px;align-items:center;flex-wrap:wrap;justify-content:center';
    container.appendChild(visuals);

    let barEl, pieEl, lineEl;
    if (showBar) {
      const barWrap = document.createElement('div');
      barWrap.style.cssText = 'flex:1 1 140px;min-width:140px;display:flex;flex-direction:column;gap:2px';
      const barOuter = document.createElement('div');
      barOuter.style.cssText = 'height:18px;background:#e4e4e7;border-radius:4px;overflow:hidden';
      barEl = document.createElement('div');
      barEl.style.cssText = 'height:100%;background:#3b82f6;transition:width 0.12s linear';
      barOuter.appendChild(barEl);
      barWrap.appendChild(barOuter);
      visuals.appendChild(barWrap);
    }
    if (showPie) {
      const svgNS = 'http://www.w3.org/2000/svg';
      const s = document.createElementNS(svgNS, 'svg');
      s.setAttribute('viewBox', '0 0 100 100');
      s.style.cssText = 'width:110px;height:110px';
      const bg = document.createElementNS(svgNS, 'circle');
      bg.setAttribute('cx', 50); bg.setAttribute('cy', 50); bg.setAttribute('r', 45);
      bg.setAttribute('fill', '#e4e4e7');
      s.appendChild(bg);
      pieEl = document.createElementNS(svgNS, 'path');
      pieEl.setAttribute('fill', '#3b82f6');
      s.appendChild(pieEl);
      const ring = document.createElementNS(svgNS, 'circle');
      ring.setAttribute('cx', 50); ring.setAttribute('cy', 50); ring.setAttribute('r', 45);
      ring.setAttribute('fill', 'none'); ring.setAttribute('stroke', '#a1a1aa'); ring.setAttribute('stroke-width', '1');
      s.appendChild(ring);
      visuals.appendChild(s);
    }
    if (showNumberLine) {
      const lineWrap = document.createElement('div');
      lineWrap.style.cssText = 'flex:1 1 100%;margin-top:8px;position:relative;height:36px';
      const track = document.createElement('div');
      track.style.cssText = 'position:absolute;top:16px;left:0;right:0;height:2px;background:#a1a1aa';
      lineWrap.appendChild(track);
      for (let i = 0; i <= denom; i++) {
        const t = document.createElement('div');
        const isEnd = (i === 0 || i === denom);
        t.style.cssText =
          `position:absolute;top:${isEnd ? 10 : 13}px;left:${(i / denom) * 100}%;` +
          `transform:translateX(-1px);width:2px;height:${isEnd ? 14 : 8}px;background:#52525b`;
        lineWrap.appendChild(t);
      }
      const l0 = document.createElement('span');
      l0.textContent = '0'; l0.style.cssText = 'position:absolute;top:28px;left:0;font-size:0.72rem;color:#52525b';
      const l1 = document.createElement('span');
      l1.textContent = '1'; l1.style.cssText = 'position:absolute;top:28px;right:0;font-size:0.72rem;color:#52525b';
      lineWrap.appendChild(l0); lineWrap.appendChild(l1);
      lineEl = document.createElement('div');
      lineEl.style.cssText =
        'position:absolute;top:9px;width:12px;height:16px;background:#3b82f6;' +
        'border-radius:3px;transform:translateX(-6px);transition:left 0.12s linear';
      lineWrap.appendChild(lineEl);
      container.appendChild(lineWrap);
    }

    function gcd(a, b) { return b === 0 ? a : gcd(b, a % b); }
    function draw() {
      const n = state.num;
      const ratio = n / denom;
      // Simplified fraction for the readout (e.g. 4/10 → 2/5)
      let simpN = n, simpD = denom;
      if (n > 0) {
        const g = gcd(n, denom);
        simpN = n / g; simpD = denom / g;
      }
      fracOut.v.textContent =
        (n === 0) ? '0' :
        (simpD === 1) ? String(simpN) :
        `${simpN}/${simpD}`;
      decOut.v.textContent = ratio.toFixed(2);
      pctOut.v.textContent = (ratio * 100).toFixed(0) + '%';
      if (barEl) barEl.style.width = (ratio * 100) + '%';
      if (pieEl) {
        // SVG arc path for ratio of a circle centered at (50,50) radius 45
        if (n === 0) { pieEl.setAttribute('d', ''); }
        else if (n === denom) {
          pieEl.setAttribute('d', 'M 50 5 A 45 45 0 1 1 49.99 5 Z');
        } else {
          const angle = ratio * 2 * Math.PI;
          const x = 50 + 45 * Math.sin(angle);
          const y = 50 - 45 * Math.cos(angle);
          const large = ratio > 0.5 ? 1 : 0;
          pieEl.setAttribute('d', `M 50 50 L 50 5 A 45 45 0 ${large} 1 ${x.toFixed(3)} ${y.toFixed(3)} Z`);
        }
      }
      if (lineEl) lineEl.style.left = (ratio * 100) + '%';
    }
    input.addEventListener('input', draw);
    draw();
  }

  // ---------------------------------------------------------------------------
  // Registry + public API
  // ---------------------------------------------------------------------------

  const WIDGETS = {
    composite_index_explorer: renderCompositeIndex,
    function_plotter: renderFunctionPlotter,
    fraction_decimal_percent: renderFractionDecimalPercent,
  };

  function render(container, mediaItem) {
    if (!container || !mediaItem) return;
    const fn = WIDGETS[mediaItem.widget_type];
    if (!fn) {
      container.textContent = 'Unknown widget type: ' + mediaItem.widget_type;
      return;
    }
    try {
      container.setAttribute('data-widget-type', mediaItem.widget_type);
      fn(container, mediaItem);
    } catch (err) {
      container.textContent = 'Widget render error: ' + (err && err.message || err);
    }
  }

  window.AfricaTutorWidgets = {
    render,
    compileExpression,           // exposed for tests
    _ALLOWED_FUNCS: ALLOWED_FUNCS,
    _ALLOWED_CONSTS: ALLOWED_CONSTS,
    _WIDGETS: WIDGETS,
  };
})();
