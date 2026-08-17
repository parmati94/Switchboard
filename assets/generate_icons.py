"""Switchboard icons: a jack field with one cord patched across it.

Two jacks side by side over a sagging cord reads as a smiley face at every size,
so the field is 3x3 and the patched pair sits on a diagonal. The unused holes are
deliberately low-contrast: they give the mark detail at 1024 and dissolve into the
panel at 24, leaving a clean diagonal cord.

Ships two masks from one artwork: a rounded square (Discord application icon) and
a circle (bot avatar).
"""
from PIL import Image, ImageDraw
import os

S, SIZE = 4, 1024
W = SIZE * S
OUT = os.path.dirname(os.path.abspath(__file__))
os.makedirs(OUT, exist_ok=True)

GROUND_EDGE = (15, 17, 15)
GROUND_MID  = (40, 47, 41)
BRASS       = (203, 146, 44)
BRASS_HI    = (240, 199, 116)
BRASS_DIM   = (120, 82, 18)
RIM         = (74, 57, 29)
HOLE        = (10, 12, 10)
HOLE_RING   = (63, 52, 32)

K = 1.18                      # content scale, so the mark fills a square frame
CORNER = 0.225                # rounded-square radius as a fraction of the side


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def sc(v):
    """Scale a 1024-space coordinate about the centre, then to supersample space."""
    return (512 + (v - 512) * K) * S


def sz(v):
    return v * K * S


def bezier(p0, p1, p2, p3, n=180):
    pts = []
    for i in range(n + 1):
        t = i / n; u = 1 - t
        pts.append((u**3*p0[0] + 3*u*u*t*p1[0] + 3*u*t*t*p2[0] + t**3*p3[0],
                    u**3*p0[1] + 3*u*u*t*p1[1] + 3*u*t*t*p2[1] + t**3*p3[1]))
    return pts


# --- artwork ----------------------------------------------------------------
img = Image.new("RGB", (W, W), GROUND_EDGE)
d = ImageDraw.Draw(img)
cx = cy = W / 2

steps, maxr = 460, W * 0.80
for i in range(steps, 0, -1):
    t = i / steps; r = maxr * t
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=lerp(GROUND_MID, GROUND_EDGE, t))

XS, YS = (300, 512, 724), (340, 512, 684)
PLUG_A = (sc(300), sc(684))          # lower-left
PLUG_B = (sc(724), sc(340))          # upper-right

for gx in XS:
    for gy in YS:
        c = (sc(gx), sc(gy))
        if c in (PLUG_A, PLUG_B):
            continue
        r = sz(48)
        d.ellipse([c[0]-r, c[1]-r, c[0]+r, c[1]+r], fill=HOLE, outline=HOLE_RING,
                  width=int(sz(7)))

curve = bezier(PLUG_A, (sc(410), sc(800)), (sc(620), sc(520)), PLUG_B)
d.line([(x, y + sz(8)) for x, y in curve], fill=BRASS_DIM, width=int(sz(44)), joint="curve")
d.line(curve, fill=BRASS, width=int(sz(44)), joint="curve")
d.line([(x, y - sz(9)) for x, y in curve], fill=BRASS_HI, width=int(sz(9)), joint="curve")


def jack(c):
    x, y = c
    ro, ring = sz(86), sz(28)
    d.ellipse([x-ro, y-ro, x+ro, y+ro], fill=HOLE, outline=BRASS, width=int(ring))
    d.arc([x-ro+ring/2, y-ro+ring/2, x+ro-ring/2, y+ro-ring/2],
          start=185, end=305, fill=BRASS_HI, width=int(ring*0.40))
    r2 = sz(25)
    d.ellipse([x-r2, y-r2, x+r2, y+r2], fill=BRASS)
    d.ellipse([x-r2, y-r2, x+sz(13), y+sz(9)], fill=BRASS_HI)


jack(PLUG_A); jack(PLUG_B)

art = img.convert("RGBA")


# --- masks ------------------------------------------------------------------
def rounded_square(source):
    out = source.copy()
    dd = ImageDraw.Draw(out)
    inset, rad = 14*S, CORNER*W
    dd.rounded_rectangle([inset, inset, W-inset, W-inset], radius=rad-inset*0.6,
                         outline=RIM, width=9*S)
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W-1, W-1], radius=rad, fill=255)
    out.putalpha(mask)
    return out


def circle(source):
    out = source.copy()
    dd = ImageDraw.Draw(out)
    rr = 496*S
    dd.ellipse([cx-rr, cy-rr, cx+rr, cy+rr], outline=RIM, width=9*S)
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, W-1, W-1], fill=255)
    out.putalpha(mask)
    return out


app = rounded_square(art).resize((SIZE, SIZE), Image.LANCZOS)
avatar = circle(art).resize((SIZE, SIZE), Image.LANCZOS)

for px in (1024, 512, 256, 128):
    app.resize((px, px), Image.LANCZOS).save(f"{OUT}/switchboard-app-{px}.png")
avatar.resize((512, 512), Image.LANCZOS).save(f"{OUT}/switchboard-avatar-512.png")
avatar.save(f"{OUT}/switchboard-avatar-1024.png")

# --- preview ----------------------------------------------------------------
BG = (245, 246, 243)
sheet = Image.new("RGB", (1020, 560), BG)
sd = ImageDraw.Draw(sheet)

sd.text((40, 24), "APP ICON  (rounded square)", fill=(94, 99, 95))
x = 40
for px, label in ((224, "224"), (128, "128"), (80, "80"), (48, "48"), (24, "24")):
    c = app.resize((px, px), Image.LANCZOS)
    sheet.paste(c, (x, 60 + (224 - px)//2), c)
    sd.text((x + px//2 - len(label)*3, 300), label, fill=(140, 145, 141))
    x += px + 44

sd.text((40, 332), "AVATAR  (circle)", fill=(94, 99, 95))
x = 40
for px, label in ((160, "160"), (96, "96"), (64, "64"), (40, "40"), (24, "24")):
    c = avatar.resize((px, px), Image.LANCZOS)
    sheet.paste(c, (x, 366 + (160 - px)//2), c)
    sd.text((x + px//2 - len(label)*3, 534), label, fill=(140, 145, 141))
    x += px + 44

sheet.save(f"{OUT}/preview.png")
print("wrote:", ", ".join(sorted(os.listdir(OUT))))
