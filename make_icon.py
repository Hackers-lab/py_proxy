"""Generate the application icon (icon.png + icon.ico).

A modern 'app icon' look: a rounded-square (squircle) with the app's blue→purple
accent gradient, a soft top highlight for depth, and a clean white hub-and-spoke
LAN network mark (a central hub linked to three peers) with a small chat dot —
evoking 'LAN chat'. Rendered at 4x and downscaled for crisp anti-aliasing.
"""

from PIL import Image, ImageDraw, ImageFilter

SS = 4                      # supersample factor
S = 1024                    # final size
W = S * SS

BLUE = (59, 130, 246)       # #3b82f6  (top-left)
PURPLE = (124, 58, 237)     # #7c3aed  (bottom-right)
WHITE = (255, 255, 255)


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _gradient_fast(size, c0, c1):
    """Diagonal gradient: average of a horizontal and a vertical linear ramp."""
    horiz = Image.new("RGB", (size, 1))
    for x in range(size):
        horiz.putpixel((x, 0), _lerp(c0, c1, x / (size - 1)))
    horiz = horiz.resize((size, size))
    vert = Image.new("RGB", (1, size))
    for y in range(size):
        vert.putpixel((0, y), _lerp(c0, c1, y / (size - 1)))
    vert = vert.resize((size, size))
    return Image.blend(horiz, vert, 0.5)


def build() -> Image.Image:
    grad = _gradient_fast(W, BLUE, PURPLE)

    # Rounded-square mask (squircle-ish via large corner radius).
    mask = Image.new("L", (W, W), 0)
    md = ImageDraw.Draw(mask)
    radius = int(W * 0.235)
    md.rounded_rectangle([0, 0, W - 1, W - 1], radius=radius, fill=255)

    icon = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    icon.paste(grad, (0, 0), mask)

    # Soft top highlight for a glossy, lit-from-above feel.
    hi = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hi)
    hd.rounded_rectangle([0, 0, W - 1, int(W * 0.5)],
                         radius=radius, fill=(255, 255, 255, 38))
    hi = hi.filter(ImageFilter.GaussianBlur(W * 0.03))
    icon = Image.alpha_composite(icon, Image.composite(
        hi, Image.new("RGBA", (W, W), (0, 0, 0, 0)), mask))

    # ── glyph: hub-and-spoke LAN network ────────────────────────────────────
    def P(x, y):
        return (int(x * W), int(y * W))

    hub = P(0.50, 0.52)
    peers = [P(0.50, 0.27), P(0.26, 0.70), P(0.74, 0.70)]
    r_hub = int(W * 0.072)
    r_peer = int(W * 0.050)
    line_w = int(W * 0.030)

    # Drop shadow (drawn first, blurred, offset down).
    shadow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)

    def draw_net(d, color):
        for p in peers:
            d.line([hub, p], fill=color, width=line_w)
        for p in peers:
            d.ellipse([p[0] - r_peer, p[1] - r_peer, p[0] + r_peer, p[1] + r_peer],
                      fill=color)
        d.ellipse([hub[0] - r_hub, hub[1] - r_hub, hub[0] + r_hub, hub[1] + r_hub],
                  fill=color)

    off = int(W * 0.012)
    # shadow uses the same geometry shifted down
    shub = (hub[0], hub[1] + off)
    speers = [(p[0], p[1] + off) for p in peers]
    for p in speers:
        sd.line([shub, p], fill=(20, 20, 40, 130), width=line_w)
    for p in speers:
        sd.ellipse([p[0] - r_peer, p[1] - r_peer, p[0] + r_peer, p[1] + r_peer],
                   fill=(20, 20, 40, 130))
    sd.ellipse([shub[0] - r_hub, shub[1] - r_hub, shub[0] + r_hub, shub[1] + r_hub],
               fill=(20, 20, 40, 130))
    shadow = shadow.filter(ImageFilter.GaussianBlur(W * 0.012))
    icon = Image.alpha_composite(icon, Image.composite(
        shadow, Image.new("RGBA", (W, W), (0, 0, 0, 0)), mask))

    # White network on top.
    glyph = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glyph)
    draw_net(gd, WHITE)
    # accent dot in the hub centre for a touch of depth
    rc = int(W * 0.030)
    gd.ellipse([hub[0] - rc, hub[1] - rc, hub[0] + rc, hub[1] + rc], fill=BLUE)
    icon = Image.alpha_composite(icon, glyph)

    return icon.resize((S, S), Image.LANCZOS)


def main() -> None:
    icon = build()
    icon.save("icon.png")
    sizes = [16, 24, 32, 48, 64, 128, 256]
    icon.save("icon.ico", sizes=[(s, s) for s in sizes])
    print("Wrote icon.png (1024) and icon.ico", sizes)


if __name__ == "__main__":
    main()
