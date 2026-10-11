#!/usr/bin/env python3
"""Draws the landing page's placeholder art as SVG, using the film's Bloop cast.

Prints the inline SVG snippet for the safe-by-design card 1 illustration (to .scratch/snippets.txt).
poster() and habits() drew the placeholder film art before the film was delivered; they are kept but no longer run.

Source of truth for the cast and the palette: the film's DESIGN-BIBLE.md (Bloop construction,
props held or placed and never strapped to the body, key light upper left, palette roles).
Stdlib only:  python3 site/tools/make-art.py
"""
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)

C = dict(
    canvas="#101C2D", world="#1C3044", detail="#304B5D", deep="#16263A", wlight="#3E5D70",
    text="#F3EEE3", text2="#B3C8D0",
    mess="#B9A27C", messShade="#8A7556", messLight="#D8C7A3", messDeep="#5F523F", paper="#E4D8BC",
    mint="#69D7B5", mintShade="#329986", mintLight="#A8EBD5",
    lamp="#FFE7B8", screen="#0E1A2A",
    file="#A7AFAE", fileShade="#7F8A8A", final="#E6E4DC",
)
AGENT = {
    "claude": dict(main="#F28C4E", shade="#C4612F", light="#F9BC90", deep="#8C4322", glow="#FFC79A"),
    "codex": dict(main="#5E9EF2", shade="#3A6CC2", light="#A3C8F8", deep="#244781", glow="#B4D4FF"),
}

H, HW = 136, 63
_uid = [0]


def uid(p):
    _uid[0] += 1
    return f"{p}{_uid[0]}"


def bloop(kind="claude", x=0, y=0, s=1.0, expr="open", arms=(10, 10), ant=0.2, look=0.0, shadow=True,
          hold=None, flip=False):
    """One Bloop standing on the ground at (x, y). arms = (left, right) angle in degrees from hanging
    down (0) through horizontal (90) to raised (170). hold = {'L'|'R': svg-string drawn at that hand,
    in hand-local coordinates}. Returns (svg, hands) with hands in world coordinates."""
    a = AGENT[kind]
    base, top = -8, -8 - H
    out = []
    if shadow:
        out.append(f'<ellipse cx="0" cy="2" rx="66" ry="10" fill="{C["canvas"]}" opacity="0.42"/>')
    for sx in (-1, 1):  # feet
        out.append(f'<ellipse cx="{26 * sx}" cy="-6" rx="17" ry="9" fill="{a["shade"]}"/>')
    hands = {}
    arm_svg = []
    for side, sx, ang in (("L", -1, arms[0]), ("R", 1, arms[1])):
        shx, shy = 58 * sx, base - H * 0.42
        r = math.radians(ang)
        hx, hy = shx + 36 * sx * math.sin(r), shy + 36 * math.cos(r)
        hands[side] = (hx, hy)
        g = (f'<g transform="translate({shx:.1f} {shy:.1f}) rotate({-sx * ang:.1f})">'
             f'<rect x="-11" y="-4" width="22" height="40" rx="11" fill="{a["shade"]}"/>'
             f'<circle cx="0" cy="36" r="13" fill="{a["main"]}"/></g>')
        arm_svg.append((ang, g, side, hx, hy))
    # arms that hang go behind the body edge; raised arms in front
    for ang, g, *_ in arm_svg:
        if ang < 60:
            out.append(g)
    # antenna (lags the body; ant: -1 drooped .. 1 upright)
    tipx, tipy = 14 * (1 - ant) * (1 if not flip else -1), top - 30 * (0.55 + 0.45 * ant) - 8
    out.append(f'<path d="M0,{top + 6} Q{tipx * 0.2:.1f},{top - 14} {tipx:.1f},{tipy:.1f}" stroke="{a["shade"]}" '
               f'stroke-width="6" fill="none" stroke-linecap="round"/>')
    out.append(f'<g transform="translate({tipx:.1f} {tipy:.1f}) rotate({(1 - ant) * 18 * (1 if not flip else -1):.1f})">'
               f'<rect x="-9" y="-24" width="18" height="24" rx="4" fill="{a["glow"]}"/>'
               f'<rect x="-9" y="-24" width="18" height="24" rx="4" fill="none" stroke="{a["shade"]}" stroke-width="3"/></g>')
    # body
    body = (f"M{-HW + 1},{base} C{-HW - 7},{base - H * 0.48:.1f} {-HW * 0.74:.1f},{top} 0,{top} "
            f"C{HW * 0.74:.1f},{top} {HW + 7},{base - H * 0.48:.1f} {HW - 1},{base} Q0,{base + 12} {-HW + 1},{base} Z")
    cid = uid("b")
    out.append(f'<clipPath id="{cid}"><path d="{body}"/></clipPath>')
    out.append(f'<path d="{body}" fill="{a["main"]}"/>')
    out.append(f'<g clip-path="url(#{cid})"><ellipse cx="0" cy="{base + 8}" rx="{HW + 20}" ry="30" fill="{a["shade"]}" opacity="0.6"/>'
               f'<ellipse cx="44" cy="{base - H * 0.45:.1f}" rx="30" ry="{H * 0.7:.1f}" fill="{a["shade"]}" opacity="0.22"/></g>')
    out.append(f'<path d="M{-HW * 0.62:.1f},{top + H * 0.3:.1f} C{-HW * 0.5:.1f},{top + H * 0.12:.1f} {-HW * 0.25:.1f},{top + H * 0.04:.1f} '
               f'{-HW * 0.04:.1f},{top + H * 0.04:.1f}" stroke="{a["light"]}" stroke-width="8" fill="none" stroke-linecap="round" opacity="0.85"/>')
    # visor + eyes
    vw, vh, vy = 96, 50, top + H * 0.2
    cx, cy = look * 6, vy + vh / 2 - 2
    out.append(f'<rect x="{-vw / 2}" y="{vy:.1f}" width="{vw}" height="{vh}" rx="{vh / 2}" fill="{C["screen"]}"/>')
    out.append(f'<rect x="{-vw / 2 + 10}" y="{vy + 6:.1f}" width="{vw * 0.3:.1f}" height="5" rx="2.5" fill="#fff" opacity="0.09"/>')
    g = a["glow"]
    for sx in (-1, 1):
        e = cx + 22 * sx
        if expr == "happy":
            out.append(f'<path d="M{e - 9},{cy + 3:.1f} Q{e},{cy - 10:.1f} {e + 9},{cy + 3:.1f}" fill="none" stroke="{g}" stroke-width="5.5" stroke-linecap="round"/>')
        elif expr == "sleepy":
            out.append(f'<path d="M{e - 9},{cy:.1f} Q{e},{cy + 6:.1f} {e + 9},{cy:.1f}" fill="none" stroke="{g}" stroke-width="5" stroke-linecap="round" opacity="0.8"/>')
        elif expr == "shock":
            out.append(f'<circle cx="{e}" cy="{cy - 1:.1f}" r="11.5" fill="none" stroke="{g}" stroke-width="5"/><circle cx="{e}" cy="{cy - 1:.1f}" r="3" fill="{g}"/>')
        elif expr == "sneaky":
            out.append(f'<rect x="{e - 10}" y="{cy - 3:.1f}" width="20" height="8" rx="4" fill="{g}" transform="rotate({sx * -9} {e} {cy:.1f})"/>')
        else:
            out.append(f'<ellipse cx="{e}" cy="{cy:.1f}" rx="10" ry="12.5" fill="{g}"/><circle cx="{e - 3}" cy="{cy - 4:.1f}" r="3" fill="#fff" opacity="0.9"/>')
    if expr == "sneaky":
        out.append(f'<path d="M{cx - 6},{cy + 15:.1f} Q{cx + 2},{cy + 18:.1f} {cx + 9},{cy + 12:.1f}" stroke="{g}" stroke-width="3.5" fill="none" stroke-linecap="round"/>')
    if expr == "shock":
        out.append(f'<ellipse cx="{cx}" cy="{cy + 15:.1f}" rx="4.5" ry="4" fill="{g}"/>')
    for ang, gsvg, side, hx, hy in arm_svg:
        if ang >= 60:
            out.append(gsvg)
    for side, svg in (hold or {}).items():
        if side == "B":  # two-handed: centred between the hands, bottom edge on the hands
            hx, hy = (hands["L"][0] + hands["R"][0]) / 2, min(hands["L"][1], hands["R"][1]) - 10
        else:
            hx, hy = hands[side]
        out.append(f'<g transform="translate({hx:.1f} {hy:.1f})">{svg}</g>')
    fl = " scale(-1 1)" if flip else ""
    world_hands = {k: (x + (-v[0] if flip else v[0]) * s, y + v[1] * s) for k, v in hands.items()}
    return f'<g transform="translate({x} {y}) scale({s}){fl}">' + "".join(out) + "</g>", world_hands


# ---------- props (one design each) ----------
def box(x, y, w, h, rot=0, label=False):
    t = min(18, h * 0.18)
    return (f'<g transform="translate({x} {y}) rotate({rot})">'
            f'<ellipse cx="{w / 2}" cy="{h + 2}" rx="{w * 0.55}" ry="7" fill="{C["canvas"]}" opacity="0.38"/>'
            f'<rect width="{w}" height="{h}" rx="6" fill="{C["mess"]}"/>'
            f'<rect x="{w * 0.78}" width="{w * 0.22}" height="{h}" rx="6" fill="{C["messShade"]}"/>'
            f'<rect width="{w}" height="{t}" rx="6" fill="{C["messLight"]}"/>'
            f'<rect x="{w * 0.42}" width="{w * 0.14}" height="{h}" fill="{C["messDeep"]}" opacity="0.55"/>'
            + (f'<rect x="{w * 0.08}" y="{h * 0.5}" width="{w * 0.26}" height="{h * 0.2}" rx="3" fill="{C["paper"]}" opacity="0.8"/>' if label else "")
            + "</g>")


def frame(x, y, rot=0, s=1.0):
    return (f'<g transform="translate({x} {y}) rotate({rot}) scale({s})">'
            f'<rect width="40" height="30" rx="4" fill="{C["messLight"]}"/>'
            f'<rect x="4" y="4" width="32" height="18" rx="2" fill="{C["messShade"]}"/>'
            f'<path d="M7 20 l8 -8 l7 6 l4 -4 l8 6" fill="none" stroke="{C["messLight"]}" stroke-width="2" stroke-linejoin="round"/></g>')


def paper(x, y, rot=0, s=1.0):
    return (f'<g transform="translate({x} {y}) rotate({rot}) scale({s} {s * 0.42})">'
            f'<rect width="46" height="58" rx="3" fill="{C["paper"]}"/>'
            f'<path d="M8 14 h28 M8 24 h30 M8 34 h22" stroke="{C["messShade"]}" stroke-width="3" opacity="0.6"/></g>')


def cup(x, y, s=1.0, tipped=False):
    rot = 78 if tipped else 0
    return (f'<g transform="translate({x} {y}) rotate({rot}) scale({s})">'
            f'<path d="M-12 -30 h24 l-4 30 h-16 z" fill="{C["paper"]}"/>'
            f'<rect x="-14" y="-33" width="28" height="5" rx="2.5" fill="{C["messLight"]}"/>'
            f'<rect x="-9" y="-20" width="18" height="6" fill="{C["mess"]}"/></g>')


def bin_(x, y, s=1.0, label=True):
    return (f'<g transform="translate({x} {y}) scale({s})">'
            f'<ellipse cx="0" cy="2" rx="50" ry="9" fill="{C["canvas"]}" opacity="0.4"/>'
            f'<path d="M-40 -80 h80 l-8 80 h-64 z" fill="{C["mint"]}"/>'
            f'<path d="M22 -80 h18 l-8 80 h-12 z" fill="{C["mintShade"]}" opacity="0.7"/>'
            f'<rect x="-46" y="-88" width="92" height="14" rx="6" fill="{C["mintLight"]}"/>'
            + (f'<text x="-4" y="-34" text-anchor="middle" font-family="JetBrains Mono, Menlo, monospace" font-weight="700" font-size="15" fill="{C["canvas"]}">.scratch</text>' if label else "")
            + "</g>")


def broom(rot=0):
    """Held from the top of the handle at (0,0), bristles down. Neutral handle, mint bristles."""
    return (f'<g transform="rotate({rot})"><rect x="-4" y="-10" width="8" height="150" rx="4" fill="{C["text2"]}"/>'
            f'<path d="M-26 140 h52 l8 46 h-68 z" fill="{C["mint"]}"/><rect x="-28" y="134" width="56" height="12" rx="4" fill="{C["mintShade"]}"/></g>')


def card_overhead(w, h, lines, button=None):
    """A prompt card held overhead with both hands; its bottom edge sits on the hands at y=0, centred."""
    s = [f'<rect x="{-w / 2}" y="{-h}" width="{w}" height="{h}" rx="16" fill="{C["text"]}"/>']
    for i, (txt, size, weight, fam, col) in enumerate(lines):
        s.append(f'<text x="0" y="{-h + 38 + i * (size + 10)}" text-anchor="middle" font-family="{fam}" font-weight="{weight}" font-size="{size}" fill="{col}">{txt}</text>')
    if button:
        bw = 92
        s.append(f'<rect x="{-bw / 2}" y="-50" width="{bw}" height="36" rx="18" fill="{C["mint"]}"/>'
                 f'<text x="0" y="-26" text-anchor="middle" font-family="Avenir Next, Inter, Arial, sans-serif" font-weight="700" font-size="18" fill="{C["canvas"]}">{button}</text>')
    return "".join(s)


SANS = "Avenir Next, Inter, Arial, sans-serif"
MONO = "JetBrains Mono, SFMono-Regular, Menlo, monospace"


def svg(w, h, title, body, defs=""):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">\n'
            f'  <title>{title}</title>\n  <defs>{defs}</defs>\n{body}\n</svg>\n')


def write(rel, text):
    p = os.path.join(SITE, rel)
    with open(p, "w") as f:
        f.write(text)
    print("wrote", rel, len(text), "bytes")


# ---------- poster ----------
def poster():
    defs = (f'<radialGradient id="wall" cx="0.4" cy="0.38" r="0.8"><stop offset="0" stop-color="#25405A"/><stop offset="0.6" stop-color="{C["world"]}"/><stop offset="1" stop-color="#152539"/></radialGradient>'
            f'<radialGradient id="pool" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="{C["lamp"]}" stop-opacity="0.22"/><stop offset="1" stop-color="{C["lamp"]}" stop-opacity="0"/></radialGradient>'
            f'<linearGradient id="cone" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{C["lamp"]}" stop-opacity="0.24"/><stop offset="1" stop-color="{C["lamp"]}" stop-opacity="0"/></linearGradient>'
            f'<radialGradient id="screenGlow" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="{C["text"]}" stop-opacity="0.16"/><stop offset="1" stop-color="{C["text"]}" stop-opacity="0"/></radialGradient>'
            f'<radialGradient id="vig" cx="0.5" cy="0.48" r="0.72"><stop offset="0.6" stop-color="{C["canvas"]}" stop-opacity="0"/><stop offset="1" stop-color="#0A1320" stop-opacity="0.7"/></radialGradient>')
    b = []
    b.append(f'<rect width="1920" height="1080" fill="{C["canvas"]}"/><rect width="1920" height="790" fill="url(#wall)"/>')
    # bunting (mess family)
    pts = []
    for i in range(0, 1921, 120):
        sag = 40 * math.sin(math.pi * ((i % 480) / 480))
        pts.append((i, 40 + sag))
    b.append('<path d="M' + " L".join(f"{x},{y:.0f}" for x, y in pts) + f'" fill="none" stroke="{C["messShade"]}" stroke-width="3"/>')
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        col = (C["messLight"], C["mess"], C["paper"])[int(x1 / 120) % 3]
        b.append(f'<path d="M{mx - 22},{my - 2:.0f} L{mx + 22},{my - 2:.0f} L{mx},{my + 34:.0f} z" fill="{col}" opacity="0.85"/>')
    # window + moon
    b.append(f'<g><rect x="640" y="150" width="520" height="330" rx="26" fill="#0E1828"/>'
             f'<circle cx="1050" cy="230" r="42" fill="{C["text"]}" opacity="0.92"/><circle cx="1070" cy="216" r="38" fill="#0E1828"/>'
             + "".join(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{C["text"]}" opacity="0.6"/>' for x, y, r in ((700, 210, 2.5), (760, 320, 2), (880, 200, 2), (960, 400, 2.5), (1120, 380, 2)))
             + f'<rect x="640" y="150" width="520" height="330" rx="26" fill="none" stroke="{C["detail"]}" stroke-width="16"/>'
             f'<path d="M900 150 V480 M640 315 H1160" stroke="{C["detail"]}" stroke-width="10"/></g>')
    # shelf (right) with jars
    b.append(f'<rect x="1500" y="300" width="330" height="14" rx="5" fill="{C["detail"]}"/>'
             f'<rect x="1530" y="234" width="34" height="66" rx="6" fill="#2A4559"/><rect x="1572" y="250" width="28" height="50" rx="6" fill="#36556A"/>'
             f'<circle cx="1660" cy="276" r="24" fill="#2A4559"/><rect x="1712" y="226" width="42" height="74" rx="8" fill="#36556A"/>')
    # desk lamp upper left = key light
    b.append(f'<path d="M150 790 L620 1080 L1100 1080 L420 330 z" fill="url(#cone)" opacity="0.8"/>'
             f'<rect x="90" y="760" width="160" height="22" rx="8" fill="{C["detail"]}"/>'
             f'<path d="M170 765 L230 520 L390 350" stroke="{C["detail"]}" stroke-width="14" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
             f'<path d="M340 300 l120 40 l-40 70 z" fill="{C["wlight"]}"/><ellipse cx="438" cy="378" rx="22" ry="12" fill="{C["lamp"]}" transform="rotate(30 438 378)"/>')
    # floor: the treemap
    tiles = [(0, 800, 620, 280, "#1A2E43"), (632, 800, 420, 130, "#22394F"), (632, 942, 200, 138, "#1E3449"),
             (844, 942, 208, 138, "#26405A"), (1064, 800, 300, 160, "#1E3449"), (1064, 972, 140, 108, "#22394F"),
             (1216, 972, 148, 108, "#1A2E43"), (1376, 800, 544, 110, "#22394F"), (1376, 922, 330, 158, "#26405A"),
             (1718, 922, 202, 158, "#1E3449")]
    b.append(f'<rect y="784" width="1920" height="18" fill="#152539"/>')
    for x, y, w, h, c in tiles:
        b.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{c}"/>')
    b.append(f'<ellipse cx="560" cy="960" rx="520" ry="120" fill="url(#pool)"/>')
    # terminal doorway
    b.append(f'<g><rect x="1030" y="720" width="120" height="30" rx="10" fill="{C["detail"]}"/><rect x="1068" y="670" width="44" height="56" fill="#2A4559"/>'
             f'<rect x="880" y="400" width="420" height="290" rx="30" fill="#0C1624"/><rect x="900" y="420" width="380" height="250" rx="18" fill="{C["screen"]}"/>'
             f'<ellipse cx="1090" cy="545" rx="260" ry="190" fill="url(#screenGlow)"/>'
             f'<path d="M934 470 l22 17 l-22 17" fill="none" stroke="{C["text2"]}" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>'
             f'<rect x="970" y="497" width="32" height="8" rx="4" fill="{C["text"]}" opacity="0.85"/>'
             f'<rect x="934" y="530" width="190" height="8" rx="4" fill="{C["text2"]}" opacity="0.3"/></g>')
    # mess: papers, cups, frames, box pile
    for x, y, r, s in ((120, 900, -12, 1.3), (300, 1010, 18, 1.2), (690, 1030, -6, 1.1), (1180, 1040, 24, 1.2), (1500, 1020, -20, 1.2), (1760, 980, 8, 1.3), (860, 900, 30, 1.0)):
        b.append(paper(x, y, r, s))
    for x, y, r, s in ((1000, 320, 18, 1.1), (1200, 360, -14, 0.9), (820, 300, -26, 0.8), (600, 520, 22, 0.9), (380, 940, -10, 1.2), (410, 925, 6, 1.2)):
        b.append(frame(x, y, r, s))
    b.append(box(1400, 760, 170, 130, 0, True) + box(1580, 790, 140, 100, 0) + box(1440, 650, 130, 110, -6) +
             box(1600, 700, 110, 90, 8) + box(1760, 850, 90, 70, -4, True))
    b.append(cup(330, 1060, 1.3) + cup(372, 1062, 1.2, True) + cup(1300, 1060, 1.3) + cup(820, 1070, 1.2))
    # gauge balloon (ochre while it fills), tied to the pile
    b.append(f'<path d="M1420 440 q30 120 70 210" fill="none" stroke="{C["text2"]}" stroke-opacity="0.5" stroke-width="3"/>'
             f'<ellipse cx="1420" cy="350" rx="74" ry="86" fill="{C["mess"]}"/><ellipse cx="1394" cy="314" rx="18" ry="28" fill="{C["messLight"]}" opacity="0.7"/>'
             f'<path d="M1410 434 l10 14 l10 -14 z" fill="{C["messShade"]}"/>'
             f'<path d="M1378 382 a42 42 0 0 1 84 0" fill="none" stroke="{C["messDeep"]}" stroke-opacity="0.35" stroke-width="10" stroke-linecap="round"/>'
             f'<path d="M1378 382 a42 42 0 0 1 76 -24" fill="none" stroke="{C["messDeep"]}" stroke-width="10" stroke-linecap="round"/>'
             f'<path d="M1420 382 L1450 362" stroke="{C["messDeep"]}" stroke-width="6" stroke-linecap="round"/><circle cx="1420" cy="382" r="7" fill="{C["messDeep"]}"/>')
    # the guests (key light upper left)
    g1, _ = bloop("claude", 560, 1010, 1.25, "happy", arms=(160, 150), ant=0.6)
    g2, h2 = bloop("codex", 830, 900, 0.95, "sneaky", arms=(15, 100), ant=-0.1, look=0.6,
                   hold={"R": frame(-8, -20, 10, 1.0)})
    g3, _ = bloop("claude", 1505, 655, 0.82, "happy", arms=(150, 165), ant=0.7)
    g4, _ = bloop("codex", 1260, 1000, 1.05, "open", arms=(30, 120), ant=0.3, look=-0.4)
    g5, _ = bloop("claude", 1140, 700, 0.55, "open", arms=(10, 140), ant=0.4)
    g6, _ = bloop("codex", 300, 860, 0.7, "sleepy", arms=(8, 8), ant=-0.9)
    zz = f'<text x="330" y="690" font-family="{SANS}" font-weight="700" font-size="34" fill="{C["text2"]}" opacity="0.8">z</text><text x="356" y="660" font-family="{SANS}" font-weight="700" font-size="24" fill="{C["text2"]}" opacity="0.6">z</text>'
    b += [g5, g6, zz, g2, g3, g4, g1]
    b.append('<rect width="1920" height="1080" fill="url(#vig)"/>')
    write("assets/film/poster.svg", svg(1920, 1080, "Afterparty film poster: orange and blue Bloop agents partying in a cosy night workshop, next to piles of boxes, papers and render frames", "\n".join(b), defs))


def habit_bg(defs_extra=""):
    defs = (f'<radialGradient id="bg" cx="0.35" cy="0.3" r="0.9"><stop offset="0" stop-color="#25405A"/><stop offset="1" stop-color="#162A3E"/></radialGradient>'
            f'<linearGradient id="beam" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="{C["lamp"]}" stop-opacity="0.5"/><stop offset="1" stop-color="{C["lamp"]}" stop-opacity="0"/></linearGradient>' + defs_extra)
    return defs, f'<rect width="640" height="360" fill="url(#bg)"/><rect y="292" width="640" height="68" fill="#1A2E43"/>'


def habits():
    # 1: one bin — a Bloop tosses a frame card into the mint bin standing beside it
    defs, bg = habit_bg()
    b1, _ = bloop("claude", 210, 312, 1.0, "happy", arms=(20, 150), ant=0.5)
    body = (bg + bin_(430, 312, 1.05) +
            f'<path d="M270 150 q80 -70 150 40" fill="none" stroke="{C["text"]}" stroke-opacity="0.35" stroke-width="3" stroke-dasharray="2 10" stroke-linecap="round"/>' +
            frame(398, 150, 28, 0.9) + b1)
    write("assets/film/habit-1-poster.svg", svg(640, 360, "A Bloop tosses a render frame into the mint .scratch bin standing beside it", body, defs))

    # 2: /tidy — the prompt card is held overhead with both hands
    defs, bg = habit_bg()
    card = card_overhead(250, 120, [("/tidy? 4.8 GB", 26, 700, MONO, C["canvas"])], "Yes")
    b2, _ = bloop("codex", 320, 318, 0.92, "open", arms=(168, 168), ant=0.4, hold={"B": card})
    cursor = f'<path d="M352 252 l0 34 l9 -9 l7 15 l7 -3 l-7 -15 l13 0 z" fill="{C["text"]}" stroke="{C["canvas"]}" stroke-width="3" stroke-linejoin="round"/>'
    body = bg + box(70, 236, 80, 60) + box(500, 248, 70, 48) + b2 + cursor
    write("assets/film/habit-2-poster.svg", svg(640, 360, "A Bloop holds a /tidy? 4.8 GB card overhead; a cursor taps Yes", body, defs))

    # 3: Sunday sweep — wall calendar, a Bloop riding the robot vacuum, holding up the question
    defs, bg = habit_bg()
    cal = (f'<g transform="translate(40 44)"><rect width="104" height="116" rx="12" fill="{C["text"]}"/>'
           f'<path d="M0 12 a12 12 0 0 1 12 -12 h80 a12 12 0 0 1 12 12 v22 h-104 z" fill="{C["detail"]}"/>'
           f'<text x="52" y="25" text-anchor="middle" font-family="{SANS}" font-weight="700" font-size="17" fill="{C["text"]}">SUN</text>'
           f'<text x="52" y="94" text-anchor="middle" font-family="{SANS}" font-weight="700" font-size="46" fill="{C["canvas"]}">7</text></g>')
    vac = (f'<g transform="translate(330 320)"><ellipse cx="0" cy="4" rx="104" ry="14" fill="{C["canvas"]}" opacity="0.4"/>'
           f'<ellipse cx="0" cy="-6" rx="100" ry="22" fill="{C["mintShade"]}"/><ellipse cx="0" cy="-14" rx="100" ry="22" fill="{C["mint"]}"/>'
           f'<ellipse cx="0" cy="-16" rx="64" ry="11" fill="{C["mintShade"]}" opacity="0.6"/></g>')
    card = card_overhead(250, 92, [("Found 12 GB.", 22, 700, SANS, C["canvas"]), ("Clean it?", 19, 500, SANS, "#456C69")])
    b3, _ = bloop("codex", 330, 300, 0.78, "open", arms=(168, 168), ant=0.4, shadow=False, hold={"B": card})
    body = bg + cal + box(500, 230, 76, 64, 6) + box(560, 250, 60, 46, -4) + vac + b3
    write("assets/film/habit-3-poster.svg", svg(640, 360, "On Sunday a Bloop rides the robot vacuum and holds up: Found 12 GB. Clean it?", body, defs))

    # 4: the map — top-down disk map; your files in warm neutrals, mess in ochre; flashlight held
    defs, _ = habit_bg()
    t = [(16, 16, 300, 200, C["file"]), (324, 16, 300, 96, C["mess"]), (324, 120, 142, 96, C["mint"]), (474, 120, 150, 96, C["messLight"]),
         (16, 224, 180, 120, C["fileShade"]), (204, 224, 112, 120, C["file"]), (324, 224, 300, 120, "#26405A")]
    tiles = "".join(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{c}" opacity="0.92"/>' for x, y, w, h, c in t)
    final = (f'<rect x="40" y="40" width="130" height="86" rx="6" fill="{C["final"]}"/>'
             f'<g transform="translate(150 58)"><rect x="-10" y="0" width="20" height="16" rx="3" fill="{C["canvas"]}"/><path d="M-6 0 v-5 a6 6 0 0 1 12 0 v5" fill="none" stroke="{C["canvas"]}" stroke-width="3"/></g>')
    dim = '<rect width="640" height="360" fill="#101C2D" opacity="0.5"/>'
    beam = '<path d="M250 236 L330 20 L640 80 L640 230 z" fill="url(#beam)"/>'
    torch = f'<g transform="rotate(-62)"><rect x="-9" y="-6" width="18" height="44" rx="5" fill="{C["detail"]}"/><rect x="-12" y="34" width="24" height="12" rx="4" fill="{C["wlight"]}"/></g>'
    b4, _ = bloop("claude", 160, 352, 0.95, "open", arms=(20, 115), ant=0.5, look=0.6, hold={"R": torch})
    body = f'<rect width="640" height="360" fill="{C["canvas"]}"/>' + tiles + final + dim + beam + b4
    write("assets/film/habit-4-poster.svg", svg(640, 360, "A Bloop with a flashlight lights up blocks on the disk map", body, defs))


def favicon():
    b, _ = bloop("claude", 32, 49, 0.25, "happy", arms=(25, 25), ant=0.6, shadow=False)
    body = (f'<rect width="64" height="64" rx="15" fill="{C["canvas"]}"/>' +
            f'<path d="M9 50 q23 12 46 0" fill="none" stroke="{C["mint"]}" stroke-width="5" stroke-linecap="round"/>' + b)
    write("assets/favicon.svg", svg(64, 64, "Afterparty", body))
    return body


def card_bloop():
    # broom held by the handle, bristles down, swung against the dome: the bonk
    g, _ = bloop("claude", 284, 122, 0.42, "sneaky", arms=(75, 20), ant=0.2, look=-0.6, hold={"L": f'<g transform="scale(0.7)">{broom(84)}</g>'})
    return g


if __name__ == "__main__":
    # poster() and habits() drew the placeholder film art; the page now uses the film's own poster and stills.
    # favicon() drew the old Bloop mark; the page now uses the app icon (tools/make-icons.py).
    os.makedirs(os.path.join(SITE, ".scratch"), exist_ok=True)
    with open(os.path.join(SITE, ".scratch", "snippets.txt"), "w") as f:
        f.write("CARD1_BLOOP\n" + card_bloop() + "\n")
    print("wrote .scratch/snippets.txt")
