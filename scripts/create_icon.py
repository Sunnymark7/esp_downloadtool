import os

from PIL import Image, ImageDraw


def rounded_rectangle(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def create_icon():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    assets_dir = os.path.join(root, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    size = 1024
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    rounded_rectangle(draw, (64, 64, 960, 960), 210, "#0f766e")
    rounded_rectangle(draw, (104, 104, 920, 920), 176, "#14b8a6")
    rounded_rectangle(draw, (168, 168, 856, 856), 140, "#062f2d")

    chip = (292, 258, 732, 698)
    rounded_rectangle(draw, chip, 54, "#e7f7f2", "#ccfbf1", 14)
    rounded_rectangle(draw, (348, 314, 676, 642), 34, "#0f766e")

    pin_fill = "#ccfbf1"
    for x in range(330, 700, 70):
        rounded_rectangle(draw, (x, 206, x + 34, 268), 12, pin_fill)
        rounded_rectangle(draw, (x, 688, x + 34, 750), 12, pin_fill)
    for y in range(300, 640, 70):
        rounded_rectangle(draw, (230, y, 292, y + 34), 12, pin_fill)
        rounded_rectangle(draw, (732, y, 794, y + 34), 12, pin_fill)

    arrow = [
        (512, 352),
        (512, 536),
        (430, 536),
        (512, 628),
        (594, 536),
        (512, 536),
        (512, 352),
    ]
    draw.polygon(arrow, fill="#ffffff")
    rounded_rectangle(draw, (394, 654, 630, 700), 18, "#ffffff")

    highlight = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    hdraw = ImageDraw.Draw(highlight)
    hdraw.ellipse((-180, -240, 700, 520), fill=(255, 255, 255, 42))
    image.alpha_composite(highlight)

    png_path = os.path.join(assets_dir, "esp32_flasher.png")
    ico_path = os.path.join(assets_dir, "esp32_flasher.ico")
    image.save(png_path)
    image.save(ico_path, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print(ico_path)


if __name__ == "__main__":
    create_icon()
