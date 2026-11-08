"""Regenerate the extension's toolbar icons: python scripts/generate_icons.py"""
from pathlib import Path

from PIL import Image, ImageDraw

SIZES = (16, 32, 48, 128)
OUT = Path(__file__).resolve().parent.parent / "extension" / "icons"
BASE = 512


def draw_master() -> Image.Image:
    img = Image.new("RGBA", (BASE, BASE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((16, 16, BASE - 16, BASE - 16), radius=112, fill=(79, 70, 229))
    # Note page with a folded corner.
    d.polygon([(150, 104), (300, 104), (370, 174), (370, 408), (150, 408)], fill=(255, 255, 255))
    d.polygon([(300, 104), (300, 174), (370, 174)], fill=(199, 210, 254))
    for y in (220, 270, 320):
        d.rounded_rectangle((188, y, 334, y + 22), radius=11, fill=(79, 70, 229))
    # Play badge: notes taken from a video.
    d.ellipse((300, 300, 452, 452), fill=(239, 68, 68), outline=(255, 255, 255), width=14)
    d.polygon([(356, 340), (356, 412), (414, 376)], fill=(255, 255, 255))
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    master = draw_master()
    for size in SIZES:
        master.resize((size, size), Image.LANCZOS).save(OUT / f"icon{size}.png")
    print(f"wrote {len(SIZES)} icons to {OUT}")


if __name__ == "__main__":
    main()
