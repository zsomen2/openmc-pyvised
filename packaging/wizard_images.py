"""Wizard images of the installer, drawn from the app logo.

The large image sits on the left of the welcome and finish pages, the small one at the top
right of the other pages; Setup picks the size that fits the display scaling (100-250 %).
Transparent, so they suit the light and the dark wizard. `just installer` writes them to
build/installer, where installer/pyvised.iss expects them.
"""

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter

ROOT = Path(__file__).resolve().parents[1]
LOGO = ROOT / "app" / "assets" / "pyvised_logo_transparent.png"
LARGE = [(202, 386), (269, 515), (336, 643), (430, 824)]
"""Image area of the modern wizard at 100, 125, 150 and 200 % (Inno Setup's documentation)."""
SMALL = [58, 77, 97, 124, 159]
"""The small image area at 100, 125, 150, 200 and 250 %."""


def large_image(logo: QImage, width: int, height: int) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    scaled = logo.scaledToWidth(round(width * 0.78), Qt.TransformationMode.SmoothTransformation)
    painter = QPainter(image)
    painter.drawImage((width - scaled.width()) // 2, round(height * 0.16), scaled)
    painter.end()
    return image


def main(out: Path) -> None:
    app = QGuiApplication([sys.argv[0], "-platform", "offscreen"])
    logo = QImage(str(LOGO))
    if logo.isNull():
        raise SystemExit(f"cannot read {LOGO}")
    out.mkdir(parents=True, exist_ok=True)
    for width, height in LARGE:
        large_image(logo, width, height).save(str(out / f"wizard-{width}.png"))
    for size in SMALL:
        small = logo.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        small.save(str(out / f"wizard-small-{size}.png"))
    del app


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "installer")
