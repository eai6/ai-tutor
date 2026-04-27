"""Geography diagram templates.

These are mostly hand-authored static SVGs. Most have no spec
parameters — the figure is constant. A few accept labels or markers.
"""

from __future__ import annotations

import math
from typing import List, Optional

from . import _common as c


# ─── Earth's layers ───────────────────────────────────────────────────

def earth_layers(spec: dict) -> Optional[str]:
    """Concentric labelled rings: inner core, outer core, mantle, crust."""
    title = spec.get('title') or "Earth's layers"
    W, H = 380, 320
    cx, cy = W / 2, H / 2 + 12

    layers = [
        # (radius, fill, label, label_offset_y)
        (130, '#fef3c7', 'Crust', -125),
        (115, '#fed7aa', 'Mantle', -100),
        (75,  '#fdba74', 'Outer core', -55),
        (35,  '#dc2626', 'Inner core', 0),
    ]
    parts = [c.svg_open(W, H), c.title(title, x=W // 2)]
    for r, fill, _, _ in layers:
        parts.append(c.circle(cx, cy, r, fill=fill, stroke=c.STROKE, width=1.2))
    # Label lines + text on the right
    label_x = W - 30
    for r, fill, label, oy in layers:
        ly = cy + oy
        parts.append(c.line(cx + r * 0.7, ly, label_x - 4, ly, stroke=c.MUTED, width=1))
        parts.append(c.label(label, x=label_x, y=ly + 4, anchor='end', size=12, weight=600))
    parts.append(c.svg_close())
    return ''.join(parts)


# ─── Volcano cross-section ────────────────────────────────────────────

def volcano_cross(spec: dict) -> Optional[str]:
    title = spec.get('title') or 'Volcano cross-section'
    W, H = 420, 300
    parts = [c.svg_open(W, H), c.title(title, x=W // 2)]
    # Mountain (triangle with crater notch)
    parts.append(c.polygon([
        (40, 250), (200, 80), (210, 95), (220, 80), (380, 250),
    ], fill='#a3a3a3', stroke=c.STROKE))
    # Magma chamber (oval below)
    parts.append('<ellipse cx="210" cy="280" rx="80" ry="14" fill="#dc2626" stroke="#7f1d1d" stroke-width="1.5"/>')
    # Vent (red strip from chamber to crater)
    parts.append(c.path("M 210 270 L 205 95 L 215 95 L 215 270 Z",
                         fill='#dc2626', stroke='#7f1d1d', width=1.2))
    # Lava flow
    parts.append(c.path("M 205 88 Q 240 110 280 200", stroke='#f97316', width=4))
    # Ash cloud
    parts.append('<ellipse cx="200" cy="55" rx="60" ry="20" fill="#737373" opacity="0.7"/>')
    parts.append('<ellipse cx="160" cy="40" rx="40" ry="14" fill="#737373" opacity="0.6"/>')
    # Labels
    labels = [
        ('Ash cloud', 130, 35),
        ('Crater', 290, 90),
        ('Vent', 280, 170),
        ('Magma chamber', 210, 295),
        ('Lava flow', 320, 200),
    ]
    for txt, x, y in labels:
        parts.append(c.label(txt, x=x, y=y, color=c.STROKE, weight=600, size=11))
    parts.append(c.svg_close())
    return ''.join(parts)


# ─── Plate boundary ───────────────────────────────────────────────────

def plate_boundary(spec: dict) -> Optional[str]:
    """Spec: type ∈ {convergent, divergent, transform}"""
    typ = (spec.get('type') or 'convergent').lower()
    if typ not in ('convergent', 'divergent', 'transform'):
        return None
    title = spec.get('title') or f"{typ.title()} plate boundary"
    W, H = 440, 280
    parts = [c.svg_open(W, H), c.title(title, x=W // 2)]
    # Mantle
    parts.append(c.rect(0, 180, W, 100, fill='#fed7aa', stroke='none'))
    # Two plates as slabs
    if typ == 'convergent':
        # left plate over (continental), right plate subducting
        parts.append(c.polygon([(0, 100), (210, 100), (200, 130), (0, 200)],
                                fill='#a3a3a3', stroke=c.STROKE))
        parts.append(c.polygon([(220, 80), (W, 80), (W, 200), (260, 200), (240, 145)],
                                fill='#737373', stroke=c.STROKE))
        # Arrows toward each other
        parts.append(c.path("M 90 80 L 130 80 L 125 73 M 130 80 L 125 87",
                             stroke=c.ACCENT, width=2))
        parts.append(c.path("M 350 70 L 310 70 L 315 63 M 310 70 L 315 77",
                             stroke=c.ACCENT, width=2))
        parts.append(c.label('Subduction zone', x=240, y=170, color=c.STROKE, weight=600, size=11))
    elif typ == 'divergent':
        parts.append(c.polygon([(0, 100), (200, 100), (200, 200), (0, 200)],
                                fill='#a3a3a3', stroke=c.STROKE))
        parts.append(c.polygon([(240, 100), (W, 100), (W, 200), (240, 200)],
                                fill='#a3a3a3', stroke=c.STROKE))
        # Magma rising
        parts.append(c.path("M 220 230 L 220 110 L 200 100 M 220 110 L 240 100",
                             stroke='#dc2626', width=3))
        # Arrows
        parts.append(c.path("M 130 80 L 90 80 L 95 73 M 90 80 L 95 87", stroke=c.ACCENT, width=2))
        parts.append(c.path("M 310 80 L 350 80 L 345 73 M 350 80 L 345 87", stroke=c.ACCENT, width=2))
        parts.append(c.label('Mid-ocean ridge / rift', x=W // 2, y=240, color=c.STROKE, weight=600, size=11))
    else:  # transform
        parts.append(c.polygon([(0, 100), (200, 100), (200, 200), (0, 200)],
                                fill='#a3a3a3', stroke=c.STROKE))
        parts.append(c.polygon([(220, 100), (W, 100), (W, 200), (220, 200)],
                                fill='#737373', stroke=c.STROKE))
        # Arrows along the fault
        parts.append(c.path("M 60 150 L 180 150 L 175 144 M 180 150 L 175 156", stroke=c.ACCENT, width=2.5))
        parts.append(c.path("M 360 150 L 240 150 L 245 144 M 240 150 L 245 156", stroke=c.ACCENT, width=2.5))
        parts.append(c.label('Transform fault', x=W // 2, y=240, color=c.STROKE, weight=600, size=11))
    parts.append(c.label('Plate', x=100, y=160, color='#fff', weight=600, size=11))
    parts.append(c.label('Plate', x=W - 100, y=160, color='#fff', weight=600, size=11))
    parts.append(c.svg_close())
    return ''.join(parts)


# ─── River profile ────────────────────────────────────────────────────

def river_profile(spec: dict) -> Optional[str]:
    """Spec: stage ∈ {upper, middle, lower} — shows characteristic profile."""
    stage = (spec.get('stage') or 'upper').lower()
    title = spec.get('title') or f"River {stage} course"
    W, H = 420, 280
    parts = [c.svg_open(W, H), c.title(title, x=W // 2)]
    if stage == 'upper':
        # V-shaped valley cross-section
        parts.append(c.polygon([(40, 80), (200, 240), (380, 80), (380, 280), (40, 280)],
                                fill='#a3a3a3', stroke=c.STROKE))
        parts.append('<ellipse cx="200" cy="232" rx="14" ry="6" fill="#3b82f6"/>')
        parts.append(c.label('V-shaped valley', x=W // 2, y=255, color='#fff', weight=600, size=12))
        parts.append(c.label('Steep gradient · vertical erosion · interlocking spurs',
                              x=W // 2, y=275, color=c.MUTED, size=11))
    elif stage == 'middle':
        # Shallow valley with meander
        parts.append(c.polygon([(40, 130), (40, 280), (380, 280), (380, 130),
                                 (320, 160), (260, 200), (200, 230), (140, 200), (90, 160)],
                                fill='#a3a3a3', stroke=c.STROKE))
        parts.append(c.path("M 60 200 Q 130 180 200 230 T 360 200", stroke='#3b82f6', width=10, fill='none'))
        parts.append(c.label('Meandering river', x=W // 2, y=265, color='#fff', weight=600, size=12))
    else:  # lower
        # Wide flat floodplain + delta
        parts.append(c.rect(40, 180, 340, 100, fill='#a3a3a3', stroke=c.STROKE))
        parts.append(c.path("M 40 220 L 280 220 L 290 200 L 320 215 L 340 200 L 360 220 L 380 220",
                             stroke='#3b82f6', width=10))
        parts.append(c.path("M 290 220 L 305 235 L 290 248 L 270 240 Z",
                             fill='#3b82f6', stroke='#3b82f6'))
        parts.append(c.path("M 320 220 L 340 240 L 320 255 L 305 248 Z",
                             fill='#3b82f6', stroke='#3b82f6'))
        parts.append(c.label('Wide floodplain · delta · deposition',
                              x=W // 2, y=170, color=c.STROKE, weight=600, size=12))
    parts.append(c.svg_close())
    return ''.join(parts)


# ─── Meander → oxbow ──────────────────────────────────────────────────

def meander_oxbow(spec: dict) -> Optional[str]:
    title = spec.get('title') or 'Meander to oxbow lake formation'
    W, H = 480, 240
    parts = [c.svg_open(W, H), c.title(title, x=W // 2)]
    # Three-stage diagram: pronounced meander → neck cut-off → oxbow lake
    stages = [
        (40, 'Stage 1: Meander', "M 0 70 Q 60 40 80 80 T 120 80"),
        (180, 'Stage 2: Neck eroded', "M 0 70 Q 60 30 80 70 L 100 70 Q 120 30 140 70"),
        (320, 'Stage 3: Oxbow lake', "M 0 70 L 60 70 L 100 70 L 160 70"),
    ]
    for ox, label, _ in stages:
        # Land block
        parts.append(c.rect(ox, 60, 160, 130, fill='#dcfce7', stroke=c.STROKE))
        parts.append(c.label(label, x=ox + 80, y=210, size=11, weight=600))

    # Stage 1: looping meander
    parts.append(c.path(f"M 40 80 Q 100 60 110 110 Q 120 160 170 140 Q 200 130 200 130",
                         stroke='#3b82f6', width=10, fill='none'))
    # Stage 2: neck nearly cut
    parts.append(c.path(f"M 180 80 Q 240 60 250 110 Q 260 160 310 140 Q 340 130 340 130",
                         stroke='#3b82f6', width=10, fill='none'))
    parts.append(c.path("M 250 110 Q 280 110 310 140", stroke='#3b82f6', width=4, dash="3 3"))
    # Stage 3: separated oxbow
    parts.append(c.line(320, 100, 480, 100, stroke='#3b82f6', width=10))
    parts.append('<ellipse cx="400" cy="155" rx="40" ry="14" fill="#3b82f6" stroke="#1e40af" stroke-width="1.2"/>')
    parts.append(c.label('Oxbow lake', x=400, y=185, size=10, color=c.STROKE, weight=600))

    parts.append(c.svg_close())
    return ''.join(parts)


# ─── Coastal features ─────────────────────────────────────────────────

def coastal_features(spec: dict) -> Optional[str]:
    """Spec: feature ∈ {headland_bay, cliff_platform, spit, stack_arch}"""
    feat = (spec.get('feature') or 'headland_bay').lower()
    title = spec.get('title') or feat.replace('_', ' ').title()
    W, H = 440, 280
    parts = [c.svg_open(W, H), c.title(title, x=W // 2)]
    if feat == 'headland_bay':
        # Wavy coastline with two headlands and a bay between
        parts.append(c.path("M 0 120 Q 80 110 100 160 Q 130 220 220 200 Q 310 180 340 130 Q 380 100 440 110 L 440 280 L 0 280 Z",
                             fill='#dcfce7', stroke=c.STROKE))
        # Sea
        parts.append(c.rect(0, 0, W, 110, fill='#bfdbfe', stroke='none'))
        parts.append(c.label('Headland', x=80, y=145, size=11, weight=600))
        parts.append(c.label('Bay', x=220, y=215, size=11, weight=600))
        parts.append(c.label('Headland', x=380, y=120, size=11, weight=600))
    elif feat == 'cliff_platform':
        # Vertical cliff + wave-cut platform
        parts.append(c.polygon([(0, 0), (200, 0), (200, 180), (440, 200), (440, 280), (0, 280)],
                                fill='#a3a3a3', stroke=c.STROKE))
        # Sea
        parts.append(c.rect(200, 180, W - 200, 20, fill='#bfdbfe', stroke='none'))
        parts.append(c.label('Cliff', x=140, y=120, color='#fff', weight=600, size=12))
        parts.append(c.label('Wave-cut platform', x=320, y=195, color=c.STROKE, size=11, weight=600))
        parts.append(c.label('Sea level', x=410, y=178, color=c.STROKE, size=10, anchor='end'))
    elif feat == 'spit':
        # Coast turning, with a spit extending into the sea
        parts.append(c.path("M 0 280 L 0 120 Q 100 100 180 130 L 180 90 Q 280 100 360 130 L 440 130 L 440 280 Z",
                             fill='#dcfce7', stroke=c.STROKE))
        parts.append(c.rect(0, 0, W, 130, fill='#bfdbfe', stroke='none'))
        # Spit
        parts.append(c.path("M 180 90 L 320 100 L 350 110 Q 340 95 320 80",
                             fill='#fef3c7', stroke=c.STROKE))
        parts.append(c.label('Spit', x=270, y=98, weight=600, size=11))
        parts.append(c.label('Coast', x=80, y=200, weight=600, size=11))
    else:  # stack_arch
        parts.append(c.rect(0, 0, W, 150, fill='#bfdbfe', stroke='none'))
        parts.append(c.rect(0, 150, W, 130, fill='#a3a3a3', stroke=c.STROKE))
        # Cliff on the left
        parts.append(c.polygon([(0, 60), (140, 60), (140, 150), (0, 150)],
                                fill='#a3a3a3', stroke=c.STROKE))
        # Arch (rectangle with cut-out)
        parts.append(c.polygon([(160, 90), (220, 90), (220, 150), (160, 150)],
                                fill='#a3a3a3', stroke=c.STROKE))
        parts.append('<rect x="170" y="115" width="40" height="35" fill="#bfdbfe" stroke="#18181b" stroke-width="1.5"/>')
        # Stack
        parts.append(c.rect(260, 105, 30, 45, fill='#a3a3a3', stroke=c.STROKE))
        # Stump
        parts.append(c.rect(330, 130, 25, 20, fill='#a3a3a3', stroke=c.STROKE))
        parts.append(c.label('Cliff', x=70, y=110, color='#fff', weight=600, size=11))
        parts.append(c.label('Arch', x=190, y=80, weight=600, size=11))
        parts.append(c.label('Stack', x=275, y=95, weight=600, size=11))
        parts.append(c.label('Stump', x=342, y=125, weight=600, size=10))
    parts.append(c.svg_close())
    return ''.join(parts)


# ─── Weathering ───────────────────────────────────────────────────────

def weathering(spec: dict) -> Optional[str]:
    """Spec: type ∈ {freeze_thaw, exfoliation, chemical}"""
    typ = (spec.get('type') or 'freeze_thaw').lower()
    title = spec.get('title') or f"{typ.replace('_', ' ').title()} weathering"
    W, H = 440, 240
    parts = [c.svg_open(W, H), c.title(title, x=W // 2)]
    if typ == 'freeze_thaw':
        # Three rocks: water → freezes/expands → cracks
        for i, label_txt in enumerate(['Water enters cracks', 'Freezes & expands', 'Rock fragments break off']):
            ox = 30 + i * 140
            parts.append(c.polygon([(ox, 100), (ox + 100, 100), (ox + 110, 180), (ox - 5, 180)],
                                    fill='#a3a3a3', stroke=c.STROKE))
            if i == 0:
                parts.append(c.line(ox + 50, 105, ox + 50, 175, stroke='#3b82f6', width=1.2))
            elif i == 1:
                parts.append(c.line(ox + 45, 105, ox + 55, 175, stroke='#3b82f6', width=2.5))
                parts.append(c.line(ox + 55, 105, ox + 45, 175, stroke='#3b82f6', width=2.5))
            else:
                parts.append(c.polygon([(ox + 80, 102), (ox + 95, 105), (ox + 90, 130)],
                                        fill='#737373', stroke=c.STROKE))
            parts.append(c.label(label_txt, x=ox + 50, y=210, size=11, weight=600))
    elif typ == 'exfoliation':
        # Onion-skin layers peeling off rock
        parts.append('<path d="M 100 220 Q 220 80 340 220 Z" fill="#737373" stroke="#18181b" stroke-width="2"/>')
        for r in (30, 50, 70, 90):
            parts.append(f'<path d="M {220 - r} 215 Q 220 {215 - r * 1.3} {220 + r} 215" fill="none" stroke="#a3a3a3" stroke-width="1.5"/>')
        parts.append(c.label('Heating & cooling cracks outer layers', x=W // 2, y=235,
                              color=c.MUTED, size=11, weight=600))
    else:  # chemical
        parts.append(c.polygon([(80, 80), (360, 80), (380, 200), (60, 200)],
                                fill='#a3a3a3', stroke=c.STROKE))
        # Acid rain drops
        for x, y in [(140, 50), (200, 35), (260, 50), (320, 35)]:
            parts.append(c.path(f"M {x} {y} L {x - 6} {y + 12} L {x + 6} {y + 12} Z",
                                 fill='#3b82f6', stroke='#1e40af'))
        # Corroded surface (jagged top)
        parts.append(c.polyline([(80, 80), (110, 75), (130, 85), (180, 78), (220, 84),
                                  (260, 78), (300, 85), (340, 80), (360, 80)],
                                 stroke=c.ACCENT, width=2.5))
        parts.append(c.label('Acid rain dissolves rock surface', x=W // 2, y=225,
                              color=c.MUTED, size=11, weight=600))
    parts.append(c.svg_close())
    return ''.join(parts)


# ─── Rock cycle ───────────────────────────────────────────────────────

def rock_cycle(spec: dict) -> Optional[str]:
    title = spec.get('title') or 'The rock cycle'
    W, H = 420, 380
    cx, cy = W / 2, H / 2 + 8
    parts = [c.svg_open(W, H), c.title(title, x=W // 2)]
    # Three rock-type bubbles + magma
    nodes = [
        # (cx, cy, fill, label1, label2)
        (cx, cy - 110, '#fcd34d', 'Igneous', 'rock'),
        (cx + 110, cy + 50, '#a78bfa', 'Sedimentary', 'rock'),
        (cx - 110, cy + 50, '#34d399', 'Metamorphic', 'rock'),
        (cx, cy + 130, '#dc2626', 'Magma', ''),
    ]
    for nx, ny, col, l1, l2 in nodes:
        parts.append(c.circle(nx, ny, 50, fill=col, stroke=c.STROKE))
        parts.append(c.label(l1, x=nx, y=ny - 4, color='#fff', weight=700, size=12))
        if l2:
            parts.append(c.label(l2, x=nx, y=ny + 12, color='#fff', weight=600, size=11))

    # Arrows
    def _arrow(p1, p2, label):
        parts.append(c.path(f"M {p1[0]} {p1[1]} L {p2[0]} {p2[1]}",
                             stroke=c.STROKE, width=2))
        # arrowhead
        ang = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
        ah1 = (p2[0] - 10 * math.cos(ang - 0.4), p2[1] - 10 * math.sin(ang - 0.4))
        ah2 = (p2[0] - 10 * math.cos(ang + 0.4), p2[1] - 10 * math.sin(ang + 0.4))
        parts.append(c.polygon([p2, ah1, ah2], fill=c.STROKE, stroke=c.STROKE))
        mx = (p1[0] + p2[0]) / 2
        my = (p1[1] + p2[1]) / 2
        parts.append(c.label(label, x=mx, y=my - 6, color=c.MUTED, size=10, anchor='middle'))

    _arrow((cx + 28, cy - 80), (cx + 88, cy + 22), 'weathering')
    _arrow((cx + 88, cy + 80), (cx - 50, cy + 80), 'heat & pressure')
    _arrow((cx - 88, cy + 22), (cx - 28, cy - 80), 'melting')
    _arrow((cx, cy + 80), (cx, cy - 60), 'cools & solidifies')
    parts.append(c.svg_close())
    return ''.join(parts)


# ─── Water cycle ──────────────────────────────────────────────────────

def water_cycle(spec: dict) -> Optional[str]:
    title = spec.get('title') or 'The water cycle'
    W, H = 480, 320
    parts = [c.svg_open(W, H), c.title(title, x=W // 2)]
    # Sky
    parts.append(c.rect(0, 0, W, 200, fill='#e0f2fe', stroke='none'))
    # Ground
    parts.append(c.rect(0, 200, W, 120, fill='#dcfce7', stroke='none'))
    # Sea
    parts.append(c.rect(0, 240, 200, 80, fill='#3b82f6', stroke='none'))
    # Sun
    parts.append(c.circle(60, 50, 22, fill='#fcd34d', stroke='#f59e0b'))
    # Clouds
    parts.append('<ellipse cx="240" cy="80" rx="60" ry="24" fill="#fff" stroke="#a3a3a3"/>')
    parts.append('<ellipse cx="380" cy="100" rx="50" ry="20" fill="#fff" stroke="#a3a3a3"/>')
    # Mountain
    parts.append(c.polygon([(380, 240), (440, 140), (480, 240)], fill='#a3a3a3', stroke=c.STROKE))
    # Arrows
    def _arrow(x1, y1, x2, y2, label, color=c.STROKE):
        parts.append(c.path(f"M {x1} {y1} L {x2} {y2}", stroke=color, width=2))
        ang = math.atan2(y2 - y1, x2 - x1)
        ah1 = (x2 - 10 * math.cos(ang - 0.4), y2 - 10 * math.sin(ang - 0.4))
        ah2 = (x2 - 10 * math.cos(ang + 0.4), y2 - 10 * math.sin(ang + 0.4))
        parts.append(c.polygon([(x2, y2), ah1, ah2], fill=color, stroke=color))
        parts.append(c.label(label, x=(x1 + x2) / 2, y=(y1 + y2) / 2 - 4, color=color, weight=600, size=11))

    _arrow(110, 230, 200, 100, 'Evaporation', color=c.ACCENT)
    _arrow(255, 60, 320, 60, 'Condensation', color=c.STROKE)
    # Precipitation (raindrops)
    for x in (340, 365, 390):
        parts.append(c.path(f"M {x} 110 L {x - 4} 130 L {x + 4} 130 Z",
                             fill='#3b82f6', stroke='#1e40af'))
    parts.append(c.label('Precipitation', x=370, y=145, weight=600, size=11))
    _arrow(440, 180, 280, 250, 'Run-off', color=c.STROKE)
    _arrow(160, 270, 100, 280, 'Collection', color=c.STROKE)
    parts.append(c.svg_close())
    return ''.join(parts)


# ─── Compass rose ─────────────────────────────────────────────────────

def compass_rose(spec: dict) -> Optional[str]:
    title = spec.get('title') or 'Compass rose'
    W, H = 280, 280
    cx, cy = W / 2, H / 2 + 8
    R = 100
    parts = [c.svg_open(W, H), c.title(title, x=W // 2)]
    parts.append(c.circle(cx, cy, R, fill='#fff', stroke=c.STROKE))
    # 8-point star
    pts = []
    for i in range(8):
        ang = math.pi / 2 - i * math.pi / 4
        outer = (cx + R * math.cos(ang), cy - R * math.sin(ang))
        inner = (cx + (R * 0.35) * math.cos(ang + math.pi / 8),
                 cy - (R * 0.35) * math.sin(ang + math.pi / 8))
        pts.append(outer)
        pts.append(inner)
    parts.append(c.polygon(pts, fill='#ede9fe', stroke=c.STROKE))
    # Cardinal labels
    for label, ang in [('N', 90), ('E', 0), ('S', -90), ('W', 180)]:
        lx = cx + (R + 16) * math.cos(math.radians(ang))
        ly = cy - (R + 16) * math.sin(math.radians(ang))
        parts.append(c.label(label, x=lx, y=ly + 4, weight=700, size=14, color=c.ACCENT))
    parts.append(c.svg_close())
    return ''.join(parts)


# ─── Seychelles map ───────────────────────────────────────────────────

def seychelles_map(spec: dict) -> Optional[str]:
    """Schematic outline of the main Seychelles islands. Spec.markers? = [{lat, lon, label}]
    is rendered as labelled dots — coordinate space is approximate
    (this is a schematic, not a survey-accurate map)."""
    title = spec.get('title') or 'Seychelles (schematic)'
    W, H = 480, 320
    parts = [c.svg_open(W, H), c.title(title, x=W // 2)]
    # Sea
    parts.append(c.rect(0, 30, W, H - 30, fill='#bfdbfe', stroke='none'))
    # Mahé (main island, granite)
    parts.append('<path d="M 180 90 Q 240 80 260 130 Q 270 200 230 240 Q 200 260 170 240 Q 140 220 150 170 Q 155 110 180 90 Z" '
                  'fill="#dcfce7" stroke="#18181b" stroke-width="1.5"/>')
    parts.append(c.label('Mahé', x=205, y=170, weight=700, size=12))
    # Praslin
    parts.append('<ellipse cx="350" cy="120" rx="40" ry="22" fill="#dcfce7" stroke="#18181b" stroke-width="1.2"/>')
    parts.append(c.label('Praslin', x=350, y=124, weight=700, size=11))
    # La Digue
    parts.append('<ellipse cx="410" cy="155" rx="20" ry="18" fill="#dcfce7" stroke="#18181b" stroke-width="1.2"/>')
    parts.append(c.label('La Digue', x=410, y=158, weight=600, size=10))
    # Silhouette
    parts.append('<ellipse cx="100" cy="160" rx="24" ry="30" fill="#dcfce7" stroke="#18181b" stroke-width="1.2"/>')
    parts.append(c.label('Silhouette', x=100, y=162, size=10, weight=600))
    # Compass
    parts.append(c.label('N ↑', x=W - 30, y=H - 14, color=c.MUTED, size=11, anchor='end'))
    parts.append(c.svg_close())
    return ''.join(parts)


# ─── Lat-long grid ────────────────────────────────────────────────────

def lat_long_grid(spec: dict) -> Optional[str]:
    """Spec: markers? = [{lat, lon, label}] (lat -90..90, lon -180..180)"""
    title = spec.get('title') or 'Latitude & longitude'
    markers = spec.get('markers') or []
    W, H = 440, 280
    pad = 50
    parts = [c.svg_open(W, H), c.title(title, x=W // 2)]
    plot_w = W - 2 * pad
    plot_h = H - 2 * pad - 10

    parts.append(c.rect(pad, pad, plot_w, plot_h, fill='#bfdbfe', stroke=c.STROKE))
    # Equator
    parts.append(c.line(pad, pad + plot_h / 2, pad + plot_w, pad + plot_h / 2, stroke=c.ACCENT, width=2))
    parts.append(c.label('Equator (0°)', x=pad + 4, y=pad + plot_h / 2 - 4, anchor='start', size=11, color=c.ACCENT))
    # Prime meridian
    parts.append(c.line(pad + plot_w / 2, pad, pad + plot_w / 2, pad + plot_h, stroke=c.ACCENT, width=2))
    parts.append(c.label('Prime meridian (0°)', x=pad + plot_w / 2 + 4, y=pad + 14, anchor='start', size=11, color=c.ACCENT))
    # Grid every 30°
    for lat in (-60, -30, 30, 60):
        y = pad + plot_h / 2 - lat * plot_h / 180
        parts.append(c.line(pad, y, pad + plot_w, y, stroke=c.GRID, width=1))
        parts.append(c.label(f"{lat}°", x=pad - 6, y=y + 4, anchor='end', size=10, color=c.MUTED))
    for lon in (-120, -60, 60, 120):
        x = pad + plot_w / 2 + lon * plot_w / 360
        parts.append(c.line(x, pad, x, pad + plot_h, stroke=c.GRID, width=1))
        parts.append(c.label(f"{lon}°", x=x, y=pad + plot_h + 14, size=10, color=c.MUTED))

    # Markers
    for m in markers:
        try:
            lat = float(m.get('lat'))
            lon = float(m.get('lon'))
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            continue
        x = pad + plot_w / 2 + lon * plot_w / 360
        y = pad + plot_h / 2 - lat * plot_h / 180
        parts.append(c.circle(x, y, 5, fill=c.ACCENT, stroke=c.ACCENT))
        if m.get('label'):
            parts.append(c.label(m['label'], x=x + 8, y=y - 6, anchor='start', size=11, weight=600))
    parts.append(c.svg_close())
    return ''.join(parts)


RENDERERS = {
    'earth_layers': earth_layers,
    'volcano_cross': volcano_cross,
    'plate_boundary': plate_boundary,
    'river_profile': river_profile,
    'meander_oxbow': meander_oxbow,
    'coastal_features': coastal_features,
    'weathering': weathering,
    'rock_cycle': rock_cycle,
    'water_cycle': water_cycle,
    'compass_rose': compass_rose,
    'seychelles_map': seychelles_map,
    'lat_long_grid': lat_long_grid,
}
