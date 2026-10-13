"""Render synthetic worksheet / slide images for the OCR + fusion tests."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "fixtures" / "screens"


def render(lines, path, size=(1600, 900), font_size=34, bg="white", fg="black"):
    im = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(im)
    try:
        f = ImageFont.truetype("arial.ttf", font_size)
        fb = ImageFont.truetype("arialbd.ttf", font_size + 8)
    except OSError:
        f = fb = ImageFont.load_default()
    y = 60
    for i, line in enumerate(lines):
        d.text((80, y), line, fill=fg, font=fb if i == 0 else f)
        y += int(font_size * 1.9)
    im.save(path)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    render(["Linear equations — Exercise 4B", "1. Solve 3x - 5 = 10", "2. Solve 4(x + 1) = 24",
            "3. Solve 2x + 7 = 19", "4. Find x when 5x − 3 = 2x + 9", "5. Solve x² − 5x + 6 = 0"], OUT / "worksheet.png")
    render(["Natural selection", "• Variation exists within a population", "• Some individuals have advantageous alleles",
            "• They are more likely to survive and reproduce", "• Alleles are passed to offspring"], OUT / "slide_bio.png")
    render(["Q3 a) Calculate the speed of a car that travels 120 m in 8 s.", "b) State the unit of speed."],
           OUT / "small_blurry.png", size=(700, 160), font_size=13)


if __name__ == "__main__":
    main()
