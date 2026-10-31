#!/usr/bin/env python3
"""The 1200x630 card that every shared overnightarc.com link renders as.

Until now a link to the site posted on X rendered as a bare blue URL. Every
link reply we have sent has looked like that. A card costs one file and one
meta tag and changes how all of them look from here on.

1200x630 because that is the size X and the rest crop to 1.91:1 without
cutting anything; the arc and the wordmark both sit inside the safe middle.

Reuses the arc from make_brand_images so the card, the avatar, the banner and
the favicon are all the same curve rather than four drawings of a similar
idea. Run: python brand/make_og_image.py
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_brand_images import BG, _font, draw_arc

W, H = 1200, 630
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'portal', 'og.png')


def card(path=OUT):
    img = Image.new('RGB', (W, H), BG)

    # The same dawn wash as the avatar, in the corner the arc lands in.
    wash = Image.new('RGB', (W, H), BG)
    ImageDraw.Draw(wash).ellipse([W * 0.55, -H * 0.45, W * 1.25, H * 0.75],
                                 fill=(34, 26, 30))
    img = Image.blend(img, wash.filter(ImageFilter.GaussianBlur(W * 0.08)), 0.85)

    # Arc across the upper right, leaving the lower left clear for type.
    p0 = (W * 0.50, H * 0.74)
    p1 = (W * 0.70, H * 0.06)
    p2 = (W * 0.95, H * 0.30)

    glow = Image.new('RGB', (W, H), BG)
    draw_arc(glow, p0, p1, p2, int(H * 0.085), glow=True)
    img = Image.blend(img, glow.filter(ImageFilter.GaussianBlur(H * 0.035)), 0.55)
    draw_arc(img, p0, p1, p2, int(H * 0.038))

    d = ImageDraw.Draw(img)
    r = H * 0.032
    d.ellipse([p2[0] - r, p2[1] - r, p2[0] + r, p2[1] + r], fill=(255, 226, 186))
    r2 = H * 0.013
    d.ellipse([p2[0] - r2, p2[1] - r2, p2[0] + r2, p2[1] + r2], fill=(255, 255, 255))

    x, y = 78, 300
    d.text((x, y), 'OVERNIGHT ARC', font=_font(62), fill=(232, 238, 246))
    d.text((x, y + 84), 'What moved in the US market overnight',
           font=_font(31, bold=False), fill=(142, 155, 173))
    d.text((x, y + 136), '1,931 stocks  ·  78 funds  ·  18 value chains',
           font=_font(27, bold=False), fill=(142, 155, 173))

    # A hairline in the accent, the same one the site uses for links.
    d.rectangle([x, y - 34, x + 96, y - 30], fill=(242, 164, 76))

    img.save(path, 'PNG', optimize=True)
    return path


if __name__ == '__main__':
    p = card()
    print('%s  %d bytes' % (p, os.path.getsize(p)))
