from __future__ import annotations

from pathlib import Path

from PIL import Image

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf, Gtk
except ImportError:
    gi = None
    GdkPixbuf = None
    Gdk = None
    Gtk = None

def crop_to_square(input_path: str, x: int, y: int, size: int) -> Image.Image:
    img = Image.open(input_path)
    img = img.convert("RGBA")
    cropped = img.crop((x, y, x + size, y + size))
    return cropped

def convert_to_png(input_path: str, output_path: str, size: int = 128, crop_box: tuple = None) -> str:
    img = Image.open(input_path)
    img = img.convert("RGBA")

    if crop_box:
        x, y, w, h = crop_box
        img = img.crop((x, y, x + w, y + h))
    else:

        w, h = img.size
        min_dim = min(w, h)
        left = (w - min_dim) // 2
        top = (h - min_dim) // 2
        img = img.crop((left, top, left + min_dim, top + min_dim))

    img = img.resize((size, size), Image.Resampling.LANCZOS)
    img.save(output_path, "PNG")
    return output_path

def write_minecraft_server_icon(source_icon_path: str, server_dir, size: int = 64) -> str | None:
    try:
        server_dir = Path(server_dir)
        server_dir.mkdir(parents=True, exist_ok=True)
        img = Image.open(source_icon_path).convert("RGBA")
        img = img.resize((size, size), Image.Resampling.LANCZOS)
        out = server_dir / "server-icon.png"
        img.save(out, "PNG")
        return str(out)
    except Exception:
        return None

def load_pixbuf(path: str, size: int = 128) -> GdkPixbuf.Pixbuf | None:
    if GdkPixbuf is None:
        return None
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), size, size, True)
        return pixbuf
    except Exception:
        return None

def create_texture_from_file(path: str, size: int = 128) -> Gdk.Texture | None:
    if Gdk is None:
        return None
    try:
        pixbuf = load_pixbuf(path, size)
        if pixbuf:
            return Gdk.Texture.new_for_pixbuf(pixbuf)
    except Exception:
        pass
    return None

def get_default_server_icon_pixbuf(size: int = 48) -> GdkPixbuf.Pixbuf | None:
    if GdkPixbuf is None:
        return None

    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, size, size)

    pixbuf.fill(0x7C6BF0FF)
    return pixbuf
