"""
Neto App - Colores de marca
Extraídos del logo oficial (neto_app.jpeg).

Uso:
    from neto_colors import HEX, RGB, BRAND

    HEX["teal"]        # "#3A9C8E"
    RGB["blue"]        # (31, 77, 142)
    BRAND.YELLOW       # "#EFA91A"
"""

# --- Códigos HEX ---
HEX = {
    "dark":   "#1B1C20",  # Negro del texto "net"
    "teal":   "#3A9C8E",  # Segmento verde-azulado del donut
    "blue":   "#1F4D8E",  # Segmento azul del donut
    "yellow": "#EFA91A",  # Segmento amarillo del donut
    "white":  "#FFFFFF",  # Fondo
}

# --- Tuplas RGB (0-255) ---
RGB = {
    "dark":   (27, 28, 32),
    "teal":   (58, 156, 142),
    "blue":   (31, 77, 142),
    "yellow": (239, 169, 26),
    "white":  (255, 255, 255),
}

# --- RGB normalizado (0.0-1.0), útil para matplotlib ---
RGB_NORM = {name: tuple(c / 255 for c in rgb) for name, rgb in RGB.items()}


class BRAND:
    """Acceso por constantes: BRAND.DARK, BRAND.TEAL, etc."""
    DARK = HEX["dark"]
    TEAL = HEX["teal"]
    BLUE = HEX["blue"]
    YELLOW = HEX["yellow"]
    WHITE = HEX["white"]


# Paleta ordenada del donut (teal → blue → yellow)
DONUT_PALETTE = [HEX["teal"], HEX["blue"], HEX["yellow"]]


def hex_to_rgb(hex_code: str) -> tuple:
    """Convierte '#RRGGBB' a tupla (r, g, b)."""
    h = hex_code.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb: tuple) -> str:
    """Convierte (r, g, b) a '#RRGGBB'."""
    return "#{:02X}{:02X}{:02X}".format(*rgb)


if __name__ == "__main__":
    print("Colores de marca Neto App:")
    for name, hex_code in HEX.items():
        print(f"  {name:<8} {hex_code}  rgb{RGB[name]}")
