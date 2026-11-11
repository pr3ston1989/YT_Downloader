from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]


def icon():
    image = Image.new("RGBA", (256, 256))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((10, 10, 246, 246), radius=53, fill="#14243a")
    draw.rounded_rectangle((24, 24, 232, 232), radius=44, fill="#1864c2")
    draw.rounded_rectangle((111, 52, 145, 143), radius=14, fill="#ffffff")
    draw.polygon(((74, 115), (182, 115), (128, 175)), fill="#ffffff")
    draw.rounded_rectangle((70, 185, 186, 202), radius=8, fill="#9bd6ff")
    image.save(ROOT / "app.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])


if __name__ == "__main__":
    icon()
    standard_library()
