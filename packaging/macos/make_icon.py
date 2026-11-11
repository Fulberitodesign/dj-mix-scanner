"""Génère l'icône de l'app (packaging/macos/icon.icns) : disque vinyle + onde, sur fond violet."""
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024


def icon() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # fond : carré arrondi avec dégradé violet -> indigo (gabarit des icônes macOS : marge de 100 px)
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / S
        gd.line([(0, y), (S, y)], fill=(int(124 - 60 * t), int(77 - 40 * t), int(255 - 70 * t), 255))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([100, 100, S - 100, S - 100], radius=185, fill=255)
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([100, 118, S - 100, S - 82], radius=185, fill=(0, 0, 0, 90))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    # vinyle
    cx, cy, r = S // 2, 440, 235
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(20, 18, 32, 255))
    for k in range(5):
        rr = r - 30 - k * 34
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=(60, 56, 84, 255), width=4)
    d.ellipse([cx - 82, cy - 82, cx + 82, cy + 82], fill=(255, 196, 61, 255))
    d.ellipse([cx - 14, cy - 14, cx + 14, cy + 14], fill=(20, 18, 32, 255))
    # onde sonore (barres) sous le disque
    bars = 15
    for i in range(bars):
        h = 25 + 55 * abs(math.sin(i * 0.9)) * (1 - abs(i - bars / 2) / bars)
        x = 262 + i * 34
        d.rounded_rectangle([x, 815 - h, x + 20, 815 + h * 0.15], radius=10, fill=(255, 255, 255, 235))
    return img


def main(out: Path) -> None:
    img = icon()
    img.save(out.with_suffix(".png"))
    img.save(out, sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("icon.icns"))
