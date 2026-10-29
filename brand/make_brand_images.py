"""Avatar and banner for @overnightarc.

The mark is the name: an arc traced across a night, cool at the start and warm
where it lands. The US session closes at midnight in Dubai and the work is done
by dawn, so the colour runs indigo to amber along the path rather than sitting
flat. A bright terminal dot marks the close - the point the whole site is about.

No wordmark on the avatar. It renders at 48 pixels in a timeline and anything
with letters in it turns to mud at that size.

Ground is #0b0e14, the same near-black the site uses, so the profile and the
page look like one thing.
"""
import math
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = os.path.dirname(os.path.abspath(__file__))

BG = (11, 14, 20)
COOL = (58, 74, 168)     # deep indigo - the night end
MID = (138, 110, 190)    # violet through the middle
WARM = (242, 164, 76)    # dawn amber - the landing


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def ramp(t):
    """Indigo -> violet -> amber along the arc."""
    return lerp(COOL, MID, t / 0.55) if t < 0.55 else lerp(MID, WARM, (t - 0.55) / 0.45)


def bezier(p0, p1, p2, t):
    x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
    y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
    return x, y


def draw_arc(img, p0, p1, p2, width, steps=900, glow=False):
    """A gradient stroke, drawn as many short overlapping segments.

    PIL has no gradient stroke, so the curve is walked and each step drawn in
    its own colour with round caps, which hides the joins.
    """
    d = ImageDraw.Draw(img)
    for i in range(steps):
        t0, t1 = i / steps, (i + 1) / steps
        a, b = bezier(p0, p1, p2, t0), bezier(p0, p1, p2, t1)
        c = ramp(t0)
        if glow:
            c = tuple(round(v * 0.55) for v in c)
        d.line([a, b], fill=c, width=width)
        r = width / 2
        d.ellipse([b[0] - r, b[1] - r, b[0] + r, b[1] + r], fill=c)


def avatar(path, size=400):
    img = Image.new('RGB', (size, size), BG)

    # A faint wash of dawn in the corner the arc lands in.
    wash = Image.new('RGB', (size, size), BG)
    wd = ImageDraw.Draw(wash)
    wd.ellipse([size * 0.52, -size * 0.28, size * 1.35, size * 0.55],
               fill=(34, 26, 30))
    img = Image.blend(img, wash.filter(ImageFilter.GaussianBlur(size * 0.18)), 0.85)

    p0 = (size * 0.14, size * 0.78)
    p1 = (size * 0.42, size * 0.06)
    p2 = (size * 0.88, size * 0.30)

    glow = Image.new('RGB', (size, size), BG)
    draw_arc(glow, p0, p1, p2, int(size * 0.14), glow=True)
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.045))
    img = Image.blend(img, glow, 0.55)

    draw_arc(img, p0, p1, p2, int(size * 0.062))

    # The close: a bright point where the arc ends.
    d = ImageDraw.Draw(img)
    r = size * 0.052
    d.ellipse([p2[0] - r, p2[1] - r, p2[0] + r, p2[1] + r], fill=(255, 226, 186))
    r2 = size * 0.021
    d.ellipse([p2[0] - r2, p2[1] - r2, p2[0] + r2, p2[1] + r2], fill=(255, 255, 255))

    img.save(path, 'PNG')
    return path


def _font(size, bold=True):
    bolds = (r'C:\Windows\Fonts\segoeuib.ttf', r'C:\Windows\Fonts\arialbd.ttf',
             r'C:\Windows\Fonts\calibrib.ttf')
    regs = (r'C:\Windows\Fonts\segoeui.ttf', r'C:\Windows\Fonts\arial.ttf',
            r'C:\Windows\Fonts\calibri.ttf')
    for p in (bolds if bold else regs):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def banner(path, w=1500, h=500):
    img = Image.new('RGB', (w, h), BG)

    wash = Image.new('RGB', (w, h), BG)
    wd = ImageDraw.Draw(wash)
    wd.ellipse([w * 0.58, -h * 0.75, w * 1.25, h * 0.95], fill=(38, 28, 32))
    img = Image.blend(img, wash.filter(ImageFilter.GaussianBlur(120)), 0.9)

    # Flatter than the avatar's, because the frame is three times as wide.
    p0 = (w * 0.04, h * 0.86)
    p1 = (w * 0.40, h * 0.04)
    p2 = (w * 0.78, h * 0.44)

    glow = Image.new('RGB', (w, h), BG)
    draw_arc(glow, p0, p1, p2, 54, glow=True)
    glow = glow.filter(ImageFilter.GaussianBlur(26))
    img = Image.blend(img, glow, 0.6)
    draw_arc(img, p0, p1, p2, 15)

    d = ImageDraw.Draw(img)
    r = 17
    d.ellipse([p2[0] - r, p2[1] - r, p2[0] + r, p2[1] + r], fill=(255, 226, 186))
    d.ellipse([p2[0] - 7, p2[1] - 7, p2[0] + 7, p2[1] + 7], fill=(255, 255, 255))

    # X drops the avatar over the lower left and crops banners inward from the
    # sides on a phone, so the wordmark sits right of the avatar but inside the
    # middle 60% that survives the crop. Tagline in regular weight - bold at
    # this size competed with the wordmark instead of sitting under it.
    f1, f2 = _font(60), _font(24, bold=False)
    d.text((w * 0.495, h * 0.57), 'OVERNIGHT ARC', font=f1, fill=(236, 240, 248))
    d.text((w * 0.497, h * 0.755),
           'What moved on the US market overnight', font=f2, fill=(142, 153, 174))

    img.save(path, 'PNG')
    return path


if __name__ == '__main__':
    a = avatar(os.path.join(OUT, 'oa_avatar.png'))
    b = banner(os.path.join(OUT, 'oa_banner.png'))
    for p in (a, b):
        im = Image.open(p)
        print(f'  {os.path.basename(p):16} {im.size[0]}x{im.size[1]}  '
              f'{os.path.getsize(p) / 1024:.0f} KB')

    # The only test that matters for an avatar.
    Image.open(a).resize((48, 48), Image.LANCZOS).resize(
        (192, 192), Image.NEAREST).save(os.path.join(OUT, 'oa_avatar_48check.png'))
    print('  oa_avatar_48check.png  what it looks like at 48px, blown back up')
