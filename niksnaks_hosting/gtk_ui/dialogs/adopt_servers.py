"""Pick up the servers a copy of this app on another machine is already managing."""

from __future__ import annotations

import threading
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from niksnaks_hosting.shared.remote.remote_adopt import (
    RemoteServerCandidate,
    discover_remote_servers,
    import_remote_servers,
    remote_data_dirs,
)


def show_adopt_dialog(parent, manager, host, on_done=None) -> None:
    """Ask the host what it is running, then let the user choose what to take on."""
    dialog = Adw.AlertDialog()
    dialog.set_heading(_("Servers on {}").format(host.name))
    dialog.set_body(_("Looking for a Niksnaks-Hosting install on {}…").format(host.name))

    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("go", _("Import"))
    dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_response_enabled("go", False)
    dialog.set_default_response("go")
    dialog.set_close_response("cancel")

    state: dict[str, Any] = {"mode": "waiting", "rows": [], "folder": ""}

    spinner = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
    dialog.set_extra_child(spinner)

    def relabel(text: str) -> None:
        if hasattr(dialog, "set_response_label"):
            dialog.set_response_label("go", text)

    # ------------------------------------------------------------------ discovery
    def look(folder: str = "") -> None:
        state["mode"] = "waiting"
        dialog.set_response_enabled("go", False)
        dialog.set_body(_("Looking for a Niksnaks-Hosting install on {}…").format(host.name))
        dialog.set_extra_child(spinner)

        def worker() -> None:
            try:
                found, where = discover_remote_servers(manager, host, folder)
                error = ""
            except Exception as e:
                found, where, error = [], "", str(e)
            GLib.idle_add(lambda: (present(found, where, error), False)[1])

        threading.Thread(target=worker, daemon=True).start()

    def present(found: list[RemoteServerCandidate], where: str, error: str) -> None:
        if error or not where:
            state["mode"] = "ask-folder"
            dialog.set_body(
                error
                or _(
                    "No Niksnaks-Hosting install was found on {}. If it keeps its data "
                    "somewhere else, say where and this app will look again."
                ).format(host.name)
            )
            # A fresh row every time: this branch can run again after "Look again"
            # fails a second time, and re-adding one widget to a new PreferencesGroup
            # each time is reparenting it out from under whatever last held it.
            group = Adw.PreferencesGroup()
            folder_row = Adw.EntryRow(title=_("Folder on {}").format(host.name))
            suggestions = remote_data_dirs(host)
            folder_row.set_text(suggestions[0] if suggestions else "")
            group.add(folder_row)
            state["folder_row"] = folder_row
            dialog.set_extra_child(group)
            relabel(_("Look again"))
            dialog.set_response_enabled("go", True)
            return

        state["folder"] = where
        if not found:
            state["mode"] = "done"
            dialog.set_body(
                _("Niksnaks-Hosting is installed on {}, but it is not managing any servers.").format(
                    host.name
                )
            )
            dialog.set_extra_child(None)
            dialog.set_response_enabled("go", False)
            return

        state["mode"] = "choose"
        importable = [c for c in found if c.importable]
        dialog.set_body(
            _("{} is managing these. Choose the ones to control from here too.").format(host.name)
        )
        relabel(_("Import"))

        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        rows: list[tuple[RemoteServerCandidate, Gtk.CheckButton]] = []

        for candidate in found:
            row = Adw.ActionRow(title=candidate.name, subtitle=_subtitle(candidate))
            if candidate.importable:
                check = Gtk.CheckButton(active=True, valign=Gtk.Align.CENTER)
                row.add_prefix(check)
                row.set_activatable_widget(check)
                rows.append((candidate, check))
            else:
                row.set_activatable(False)
                row.set_sensitive(False)
            listbox.append(row)

        state["rows"] = rows
        scroller = Gtk.ScrolledWindow(propagate_natural_height=True, max_content_height=320)
        scroller.set_child(listbox)
        dialog.set_extra_child(scroller)
        dialog.set_response_enabled("go", bool(importable))

    # --------------------------------------------------------------------- import
    def do_import() -> None:
        chosen = [c for c, check in state["rows"] if check.get_active()]
        if not chosen:
            if on_done:
                on_done([])
            return

        def worker() -> None:
            try:
                imported, problems = import_remote_servers(manager, host, chosen)
            except Exception as e:
                imported, problems = [], [str(e)]

            def finish() -> bool:
                if problems:
                    _alert(parent, _("Some servers were not imported"), "\n".join(problems))
                if on_done:
                    on_done(imported)
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

    def on_response(_d, response):
        if response != "go":
            return
        if state["mode"] == "ask-folder":
            # The dialog closed on this press; reopen it and look where we were told.
            typed = state["folder_row"].get_text().strip()
            dialog.present(parent)
            look(typed)
            return
        if state["mode"] == "choose":
            do_import()

    dialog.connect("response", on_response)
    dialog.present(parent)
    look()


def _subtitle(candidate: RemoteServerCandidate) -> str:
    if candidate.refused:
        return candidate.refused
    if candidate.already_imported:
        return _("Already in this app")
    return candidate.summary


def _alert(parent, title: str, body: str) -> None:
    dialog = Adw.AlertDialog()
    dialog.set_heading(title)
    dialog.set_body(body)
    dialog.add_response("ok", _("OK"))
    dialog.present(parent)
