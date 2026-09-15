from __future__ import annotations

import io

from PIL import Image

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk
except Exception:
    # Not only ImportError: require_version raises ValueError when the typelib is not
    # installed, and a half-installed PyGObject leaves an importable `gi` with nothing
    # in it. The picture functions below are plain PIL and are wanted either way --
    # the web panel writes server pictures through them too.
    gi = None
    GdkPixbuf = None
    Gdk = None
    GLib = None
    Gtk = None

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

def png_bytes(source_path: str, size: int = 128) -> bytes:
    """A square PNG of *source_path* at *size*, as bytes.

    Bytes rather than a file, because the folder it is destined for may be on another
    computer: PIL can only write to this machine's disk, but bytes go anywhere a path
    object can put them.
    """
    img = Image.open(source_path).convert("RGBA")
    width, height = img.size
    side = min(width, height)
    img = img.crop(
        ((width - side) // 2, (height - side) // 2, (width - side) // 2 + side, (height - side) // 2 + side)
    )
    img = img.resize((size, size), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    return buffer.getvalue()


def write_minecraft_server_icon(source_icon_path: str, server_dir, size: int = 64) -> str | None:
    """Write the picture Minecraft shows in the multiplayer list, wherever the folder is.

    server_dir is the folder object -- a Path here, a RemotePath on a host -- and never
    rebuilt as a Path: a remote folder rewrapped that way names a file on this computer
    that either is not there or, worse, is somebody else's.
    """
    try:
        server_dir.mkdir(parents=True, exist_ok=True)
        out = server_dir / "server-icon.png"
        out.write_bytes(png_bytes(source_icon_path, size))
        return str(out)
    except Exception:
        return None


def load_texture(data: bytes):
    """A paintable from PNG bytes, for a picture that was never on this machine's disk."""
    if not data or Gdk is None:
        return None
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
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

