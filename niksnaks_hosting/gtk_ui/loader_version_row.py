import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from niksnaks_hosting.shared.backend.download_manager import (
    LoaderVersionOption,
    default_loader_option_index,
)
from niksnaks_hosting.shared.utils.constants import LOADER_FORGE, normalize_loader

def build_loader_version_row() -> Adw.ComboRow:

    row = Adw.ComboRow(
        title=_("Loader version"),
        subtitle=_("Select a Minecraft version"),
    )
    _enable_search(row)
    row.set_model(Gtk.StringList.new([_("Loading...")]))
    row.set_sensitive(False)
    return row

def _enable_search(row: Adw.ComboRow) -> None:

    if not hasattr(row, "set_enable_search"):
        return
    try:
        row.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        row.set_enable_search(True)
    except Exception:
        pass

def set_loader_version_message(row: Adw.ComboRow, message: str, subtitle: str) -> None:
    row.set_model(Gtk.StringList.new([message]))
    row.set_sensitive(False)
    row.set_subtitle(subtitle)

def set_loader_versions_loading(row: Adw.ComboRow, mc_version: str) -> None:
    set_loader_version_message(row, _("Loading..."), _("Loading versions for Minecraft {}...").format(mc_version))

def set_loader_versions(
    row: Adw.ComboRow,
    options: list[LoaderVersionOption],
    loader_type: str,
    mc_version: str,
) -> None:

    if not options:
        set_loader_version_message(row, _("No versions found"), _no_versions_subtitle(loader_type, mc_version))
        return

    row.set_model(Gtk.StringList.new([option.label for option in options]))
    row.set_selected(default_loader_option_index(options))
    row.set_sensitive(True)
    row.set_subtitle(_("Compatible with Minecraft {}").format(mc_version))

def selected_loader_version(row: Adw.ComboRow, options: list[LoaderVersionOption]) -> str:

    if not options:
        return ""

    item = row.get_selected_item()
    label = item.get_string() if item else ""
    for option in options:
        if option.label == label:
            return option.version

    idx = int(row.get_selected())
    if 0 <= idx < len(options):
        return options[idx].version
    return ""

def _no_versions_subtitle(loader_type: str, mc_version: str) -> str:
    if normalize_loader(loader_type) == LOADER_FORGE:
        return _("No Forge build for MC {}").format(mc_version)
    return _("No Fabric loader for MC {}").format(mc_version)
