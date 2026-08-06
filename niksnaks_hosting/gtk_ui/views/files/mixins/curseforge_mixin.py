from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, GLib, Gtk, Pango

from ..utils import *
from niksnaks_hosting.shared.utils.constants import get_loader_display_name

CURSEFORGE_STATE_FILE = ".niksnaks-hosting-curseforge.json"

PAGE_SIZE = 20


class CurseForgeMixin:
    # ------------------------------------------------------------ installed packs
    # CurseForge ids are plain numbers and its files have no Modrinth counterpart, so
    # these packs are tracked in their own file rather than in the Modrinth state that
    # the version-compatibility checks read.

    def _curseforge_state_path(self) -> Path | None:
        root = self._server_dir()
        if not root:
            return None
        return root / CURSEFORGE_STATE_FILE

    def _read_curseforge_state(self) -> dict[str, Any]:
        path = self._curseforge_state_path()
        if not path or not path.exists():
            return {"installed_packs": {}}
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            packs = raw.get("installed_packs") if isinstance(raw, dict) else None
            return {"installed_packs": packs if isinstance(packs, dict) else {}}
        except Exception:
            return {"installed_packs": {}}

    def _write_curseforge_state(self, state: dict[str, Any]) -> None:
        path = self._curseforge_state_path()
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except OSError:
            pass

    def _curseforge_entries(self) -> dict[str, dict[str, Any]]:
        packs = self._read_curseforge_state().get("installed_packs", {})
        out: dict[str, dict[str, Any]] = {}
        for mod_id, value in packs.items():
            key = str(mod_id).strip()
            if not key or not isinstance(value, dict):
                continue
            raw_mods = value.get("mods")
            mods = sorted(
                {
                    str(Path(str(m)).name).strip().lower()
                    for m in (raw_mods if isinstance(raw_mods, list) else [])
                    if str(m).strip().lower().endswith(".jar")
                }
            )
            out[key] = {
                "title": str(value.get("title", "")).strip(),
                "file_id": str(value.get("file_id", "")).strip(),
                "file_name": str(value.get("file_name", "")).strip(),
                "mc_version": str(value.get("mc_version", "")).strip(),
                "loader": str(value.get("loader", "")).strip(),
                "loader_version": str(value.get("loader_version", "")).strip(),
                "website_url": str(value.get("website_url", "")).strip(),
                "mods": mods,
            }
        return out

    def _is_curseforge_pack_installed(self, mod_id: str | int) -> bool:
        return str(mod_id).strip() in self._curseforge_entries()

    def _curseforge_managed_mod_names(self) -> set[str]:
        """Jar names owned by an installed pack, so they are not also listed on their own."""
        names: set[str] = set()
        for entry in self._curseforge_entries().values():
            names.update(entry.get("mods", []))
        return names

    def _record_curseforge_install(self, hit, pack_file, result) -> None:
        state = self._read_curseforge_state()
        packs = state.setdefault("installed_packs", {})
        packs[str(hit.mod_id)] = {
            "title": hit.name,
            "file_id": str(pack_file.file_id),
            "file_name": pack_file.display_name or pack_file.file_name,
            "mc_version": result.pack_mc_version,
            "loader": result.pack_loader,
            "loader_version": result.pack_loader_version,
            "website_url": hit.website_url,
            "mods": sorted(result.managed_mod_files),
        }
        self._write_curseforge_state(state)

    def _make_curseforge_pack_row(self, mod_id: str, entry: dict[str, Any]) -> Adw.ActionRow:
        title = entry.get("title") or mod_id
        mods = entry.get("mods") or []

        row = Adw.ActionRow(title=title)
        bits = ["CurseForge", _("{} managed mods").format(len(mods))]
        loader = entry.get("loader", "")
        loader_version = entry.get("loader_version", "")
        if loader and loader_version:
            bits.append(f"{loader.title()} {loader_version}")
        elif entry.get("file_name"):
            bits.append(entry["file_name"])
        row.set_subtitle(" · ".join(bits))
        row.set_activatable(False)

        row.add_suffix(
            self._icon_button(
                "view-list-symbolic",
                _("View managed mods"),
                lambda *_p, t=title, m=mods: self._show_modpack_mods_dialog(t, m),
            )
        )
        website = entry.get("website_url") or f"https://www.curseforge.com/minecraft/modpacks/{mod_id}"
        row.add_suffix(
            self._icon_button(
                "web-browser-symbolic",
                _("Open modpack page"),
                lambda *_p, url=website: _open_uri(url),
            )
        )
        row.add_suffix(
            self._icon_button(
                "user-trash-symbolic",
                _("Delete modpack"),
                lambda *_p, pid=mod_id, t=title: self._confirm_delete_curseforge_pack(pid, t),
                destructive=True,
            )
        )
        return row

    def _confirm_delete_curseforge_pack(self, mod_id: str, title: str) -> None:
        if self._is_running():
            self._alert(_("Server is running"), _("Stop the server before deleting a modpack."))
            return

        dialog = Adw.AlertDialog()
        dialog.set_heading(_("Delete modpack?"))
        dialog.set_body(_('Remove "{}" and delete its managed mod files from this server?').format(title))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_dialog, response):
            if response == "delete":
                self._delete_curseforge_pack(mod_id, title)

        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    def _delete_curseforge_pack(self, mod_id: str, title: str) -> None:
        entry = self._curseforge_entries().get(str(mod_id))
        if not entry:
            return

        root = self._server_dir()
        if not root:
            self._alert(_("No server selected"), _("Select a server before deleting a modpack."))
            return

        mods_dir = root / "mods"
        removed = 0
        for mod_name in entry.get("mods", []):
            target = self._find_mod_jar_path(mods_dir, mod_name)
            if target and target.exists():
                target.unlink(missing_ok=True)
                removed += 1
            self._remove_mod_from_mod_states(mod_name)

        state = self._read_curseforge_state()
        state.get("installed_packs", {}).pop(str(mod_id), None)
        self._write_curseforge_state(state)

        self._rebuild_lists()
        # The pack's config and scripts stay behind; only its mods are ours to remove.
        self._toast(_("Deleted {} ({} mod files removed)").format(title, removed))

    # ---------------------------------------------------------------- browse page
    def _push_curseforge_page(self, *_args) -> None:
        self._curseforge_nav = Adw.NavigationView()
        self._curseforge_nav.set_hexpand(True)
        self._curseforge_nav.set_vexpand(True)

        search_page = Adw.NavigationPage(
            title=_("CurseForge"),
            child=self._build_curseforge_search_view(),
        )
        try:
            search_page.set_tag("curseforge-search")
        except Exception:
            pass
        self._curseforge_nav.push(search_page)

        outer_page = Adw.NavigationPage(title=_("CurseForge"), child=self._curseforge_nav)
        self._curseforge_page = outer_page
        if self._push_fullscreen_page_cb:
            if self._curseforge_header:
                self._curseforge_header.set_show_end_title_buttons(True)
            self._push_fullscreen_page_cb(outer_page)
        else:
            self._nav.push(outer_page)

    def _build_curseforge_search_view(self) -> Gtk.Widget:
        from niksnaks_hosting.shared.backend import curseforge_client

        tv = Adw.ToolbarView()
        tv.set_hexpand(True)

        header = Adw.HeaderBar()
        header.add_css_class("modrinth-header")
        self._curseforge_header = header
        header.set_show_start_title_buttons(True)
        header.set_show_end_title_buttons(False)

        search_outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        search_outer.add_css_class("modrinth-search-box")
        search_outer.set_hexpand(True)
        search_outer.set_valign(Gtk.Align.CENTER)

        entry = Gtk.SearchEntry()
        entry.set_hexpand(True)
        entry.add_css_class("modrinth-search-entry")
        search_outer.append(entry)

        search_spinner = Gtk.Spinner()
        search_spinner.set_valign(Gtk.Align.CENTER)
        search_spinner.set_margin_end(4)
        search_spinner.set_visible(False)
        search_outer.append(search_spinner)

        filter_btn = Gtk.MenuButton()
        filter_btn.set_icon_name("sliders-horizontal-symbolic")
        filter_btn.add_css_class("flat")
        filter_btn.add_css_class("modrinth-filter-btn")
        filter_btn.set_tooltip_text(_("Filters"))
        search_outer.append(filter_btn)

        header.set_title_widget(search_outer)
        tv.add_top_bar(header)

        mc_version = self._server_info.mc_version if self._server_info else ""
        loader = self._server_loader()
        entry.set_placeholder_text(
            _("Search {} {} modpacks…").format(get_loader_display_name(loader), mc_version)
            if mc_version
            else _("Search {} modpacks…").format(get_loader_display_name(loader))
        )

        sort_items = [
            (_("Popularity"), curseforge_client.SORT_POPULARITY),
            (_("Downloads"), curseforge_client.SORT_TOTAL_DOWNLOADS),
            (_("Recently updated"), curseforge_client.SORT_LAST_UPDATED),
            (_("Name"), curseforge_client.SORT_NAME),
        ]
        selected_sort_idx = [0]
        # Filled in from the API once it answers; index 0 always means "no filter".
        category_items: list[tuple[str, int]] = [(_("Any category"), 0)]
        selected_cat_idx = [0]

        def make_filter_flowbox(items, default_idx, max_cols):
            buttons = []
            group = Gtk.FlowBox()
            group.set_max_children_per_line(max_cols)
            group.set_selection_mode(Gtk.SelectionMode.NONE)
            group.set_column_spacing(4)
            group.set_row_spacing(4)
            for i, (label, _value) in enumerate(items):
                btn = Gtk.ToggleButton(label=label)
                btn.add_css_class("modrinth-filter-option")
                btn.set_active(i == default_idx)
                group.append(btn)
                buttons.append(btn)
            return group, buttons

        sort_box, sort_buttons = make_filter_flowbox(sort_items, 0, 2)

        cat_box = Gtk.FlowBox()
        cat_box.set_max_children_per_line(3)
        cat_box.set_selection_mode(Gtk.SelectionMode.NONE)
        cat_box.set_column_spacing(4)
        cat_box.set_row_spacing(4)
        cat_buttons: list[Gtk.ToggleButton] = []

        filter_popover = Gtk.Popover()
        filter_btn.set_popover(filter_popover)

        popover_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        popover_content.set_margin_start(12)
        popover_content.set_margin_end(12)
        popover_content.set_margin_top(12)
        popover_content.set_margin_bottom(12)

        cat_label = Gtk.Label(label=_("Category"), xalign=0.0)
        cat_label.add_css_class("modrinth-filter-label")
        popover_content.append(cat_label)
        popover_content.append(cat_box)

        sort_label = Gtk.Label(label=_("Sort by"), xalign=0.0)
        sort_label.add_css_class("modrinth-filter-label")
        popover_content.append(sort_label)
        popover_content.append(sort_box)

        filter_popover.set_child(popover_content)

        results = Gtk.ListBox()
        self._curseforge_results_list = results
        results.set_selection_mode(Gtk.SelectionMode.NONE)
        results.add_css_class("mod-results-list")
        results.set_vexpand(True)
        results.set_activate_on_single_click(True)
        results.connect("row-activated", self._on_curseforge_row_activated)
        results.set_margin_start(12)
        results.set_margin_end(12)
        results.set_margin_top(2)

        state = {"offset": 0, "busy": False, "all_loaded": False}

        def set_busy(busy: bool):
            state["busy"] = busy
            filter_btn.set_sensitive(not busy)
            for btn in (*sort_buttons, *cat_buttons):
                btn.set_sensitive(not busy)
            search_spinner.set_visible(busy)
            if busy:
                search_spinner.start()
            else:
                search_spinner.stop()

        def clear_results():
            while True:
                row = results.get_row_at_index(0)
                if row is None:
                    break
                results.remove(row)

        def finish_search(hits, total, err, appending: bool):
            set_busy(False)
            if err:
                if not appending:
                    clear_results()
                    results.append(self._empty_listbox_row(_("Could not fetch CurseForge results.")))
                return

            if not appending:
                clear_results()
            if not hits:
                if not appending:
                    results.append(
                        self._empty_listbox_row(
                            _("No modpacks found for Minecraft {} on {}.").format(
                                mc_version or "?", get_loader_display_name(loader)
                            )
                        )
                    )
                state["all_loaded"] = True
                return

            if total <= state["offset"] + len(hits):
                state["all_loaded"] = True

            for hit in hits:
                results.append(self._make_curseforge_row(hit))

        def do_search(reset: bool = False):
            if reset:
                state["offset"] = 0
                state["all_loaded"] = False

            query = entry.get_text().strip()
            offset = int(state["offset"])
            sort_field = sort_items[selected_sort_idx[0]][1]
            category_id = category_items[selected_cat_idx[0]][1] if selected_cat_idx[0] < len(category_items) else 0
            set_busy(True)

            def thread_fn():
                try:
                    hits, total = curseforge_client.search_modpacks(
                        query=query,
                        game_version=mc_version,
                        loader=loader,
                        category_id=category_id,
                        sort_field=sort_field,
                        index=offset,
                        page_size=PAGE_SIZE,
                    )
                    GLib.idle_add(lambda h=hits, t=total, a=not reset: finish_search(h, t, None, a))
                except Exception as ex:
                    GLib.idle_add(lambda e=str(ex), a=not reset: finish_search([], 0, e, a))

            threading.Thread(target=thread_fn, daemon=True).start()

        def do_search_more():
            if state["busy"] or state["all_loaded"]:
                return
            state["offset"] += PAGE_SIZE
            do_search(reset=False)

        def on_scroll(*_args):
            adj = sw.get_vadjustment()
            if adj.get_upper() <= adj.get_page_size():
                return
            if adj.get_value() + adj.get_page_size() >= adj.get_upper() - 300:
                do_search_more()

        def trigger_search(*_args):
            do_search(reset=True)
            return False

        entry.connect("search-changed", trigger_search)
        entry.connect("activate", trigger_search)

        def wire_filter_buttons(buttons, selected_idx_ref):
            def handle_click(btn, idx):
                if btn.get_active():
                    for j, other in enumerate(buttons):
                        if j != idx:
                            other.set_active(False)
                    selected_idx_ref[0] = idx
                    trigger_search()
                    filter_btn.set_active(False)
                else:
                    btn.set_active(True)

            for i, btn in enumerate(buttons):
                btn.connect("clicked", lambda b, idx=i: handle_click(b, idx))

        wire_filter_buttons(sort_buttons, selected_sort_idx)

        def apply_categories(loaded: list[tuple[str, int]]):
            category_items.extend(loaded)
            for i, (label, _value) in enumerate(category_items):
                btn = Gtk.ToggleButton(label=label)
                btn.add_css_class("modrinth-filter-option")
                btn.set_active(i == 0)
                cat_box.append(btn)
                cat_buttons.append(btn)
            wire_filter_buttons(cat_buttons, selected_cat_idx)

        def load_categories():
            try:
                loaded = curseforge_client.list_categories()
            except Exception:
                loaded = []
            GLib.idle_add(lambda c=loaded: apply_categories(c) or False)

        threading.Thread(target=load_categories, daemon=True).start()

        sw = Gtk.ScrolledWindow()
        sw.add_css_class("mod-scroll")
        sw.set_vexpand(True)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        clamp = Adw.Clamp()
        clamp.set_child(results)
        clamp.set_maximum_size(900)
        sw.set_child(clamp)
        sw.get_vadjustment().connect("value-changed", on_scroll)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_hexpand(True)
        outer.append(sw)

        GLib.idle_add(lambda: do_search(reset=True) or False)
        tv.set_content(outer)
        return tv

    def _on_curseforge_row_activated(self, _listbox, listbox_row):
        hit = getattr(listbox_row, "_hit", None)
        if hit is not None:
            self._curseforge_nav.push(self._build_curseforge_detail_page(hit))

    def _make_curseforge_row(self, hit) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(True)
        row.add_css_class("mod-card-row")
        row.add_css_class("card")
        row.set_margin_bottom(6)

        btn_label = _("Install")
        compact_install = Gtk.Button(label=btn_label)
        compact_install.add_css_class("mod-install-btn-fixed")
        compact_install.set_valign(Gtk.Align.CENTER)
        expanded_install = Gtk.Button(label=btn_label)
        expanded_install.add_css_class("mod-install-btn-expanded")
        row_btns = [compact_install, expanded_install]

        def set_row_btn(label=None, sensitive=None):
            for btn in row_btns:
                if label is not None:
                    btn.set_label(label)
                if sensitive is not None:
                    btn.set_sensitive(sensitive)

        if self._is_curseforge_pack_installed(hit.mod_id):
            set_row_btn(_("Installed"), False)

        def make_text_col():
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            col.set_hexpand(True)
            title = Gtk.Label(label=hit.name, xalign=0.0)
            title.add_css_class("title-4")
            title.set_wrap(False)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            col.append(title)

            meta = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
            icon = Gtk.Image.new_from_icon_name("folder-download-symbolic")
            icon.set_pixel_size(12)
            icon.add_css_class("dim-label")
            meta.append(icon)
            count = Gtk.Label(label=_format_compact_count(int(hit.downloads or 0)), xalign=0.0)
            count.add_css_class("caption")
            count.add_css_class("dim-label")
            meta.append(count)
            if hit.author:
                author = Gtk.Label(label=f"· {hit.author}", xalign=0.0)
                author.add_css_class("caption")
                author.add_css_class("dim-label")
                author.set_ellipsize(Pango.EllipsizeMode.END)
                meta.append(author)
            col.append(meta)

            summary = (hit.summary or "").strip()
            if summary:
                label = Gtk.Label(label=summary, xalign=0.0)
                label.set_wrap(True)
                label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                label.set_lines(2)
                label.set_ellipsize(Pango.EllipsizeMode.END)
                label.add_css_class("dim-label")
                label.add_css_class("caption")
                col.append(label)
            return col

        def make_icon():
            image = Gtk.Image.new_from_icon_name("package-x-generic-symbolic")
            image.set_pixel_size(48)
            image.set_valign(Gtk.Align.START)
            if hit.logo_url:
                self._load_icon_async(image, hit.logo_url, size=48)
            return image

        def make_chevron():
            chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
            chevron.set_pixel_size(16)
            chevron.add_css_class("dim-label")
            chevron.set_valign(Gtk.Align.CENTER)
            return chevron

        compact = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        compact.set_margin_start(12)
        compact.set_margin_end(6)
        compact.set_margin_top(10)
        compact.set_margin_bottom(10)
        compact.append(make_icon())
        compact.append(make_text_col())
        compact.append(compact_install)
        compact.append(make_chevron())

        expanded = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        expanded.set_margin_start(12)
        expanded.set_margin_end(6)
        expanded.set_margin_top(10)
        expanded.set_margin_bottom(10)
        expanded_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        expanded_top.append(make_icon())
        expanded_top.append(make_text_col())
        expanded_top.append(make_chevron())
        expanded.append(expanded_top)
        expanded_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        expanded_actions.set_margin_top(2)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        expanded_actions.append(spacer)
        expanded_actions.append(expanded_install)
        expanded.append(expanded_actions)

        row_stack = Gtk.Stack()
        row_stack.set_hhomogeneous(False)
        row_stack.set_vhomogeneous(False)
        row_stack.add_named(compact, "compact")
        row_stack.add_named(expanded, "expanded")
        row.set_child(row_stack)

        def tick_row(widget, _clock, _ud=None):
            width = widget.get_width()
            current = widget.get_visible_child_name()
            if width >= 550 and current != "compact":
                widget.set_visible_child_name("compact")
            elif 0 < width < 550 and current != "expanded":
                widget.set_visible_child_name("expanded")
            return True

        row_stack.add_tick_callback(tick_row)

        def on_install(*_args):
            self._start_curseforge_install(hit, None, set_row_btn)

        compact_install.connect("clicked", on_install)
        expanded_install.connect("clicked", on_install)
        row._hit = hit
        row._set_row_btn = set_row_btn
        return row

    def _build_curseforge_detail_page(self, hit) -> Adw.NavigationPage:
        from niksnaks_hosting.shared.backend import curseforge_client

        tv = Adw.ToolbarView()
        tv.set_hexpand(True)
        tv.set_vexpand(True)

        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(True)
        header.set_show_end_title_buttons(True)
        tv.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(24)
        content.set_margin_bottom(24)

        install_btn = Gtk.Button(label=_("Install"))
        install_btn.add_css_class("suggested-action")
        install_btn.add_css_class("mod-install-btn-large")
        install_btn.add_css_class("pill")
        install_btn.set_valign(Gtk.Align.START)
        install_btn.set_sensitive(False)

        def set_detail_btn(label=None, sensitive=None):
            if label is not None:
                install_btn.set_label(label)
            if sensitive is not None:
                install_btn.set_sensitive(sensitive)

        icon = Gtk.Image.new_from_icon_name("package-x-generic-symbolic")
        icon.set_pixel_size(72)
        icon.set_valign(Gtk.Align.START)
        if hit.logo_url:
            self._load_icon_async(icon, hit.logo_url, size=72)

        title_lbl = Gtk.Label(label=hit.name, xalign=0.0)
        title_lbl.add_css_class("title-1")
        title_lbl.set_wrap(True)
        title_lbl.set_hexpand(True)

        author_lbl = Gtk.Label(label=_("by {}").format(hit.author or _("Unknown")), xalign=0.0)
        author_lbl.add_css_class("title-4")
        author_lbl.add_css_class("dim-label")

        stats_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        stats_box.set_margin_top(4)
        stats_icon = Gtk.Image.new_from_icon_name("folder-download-symbolic")
        stats_icon.set_pixel_size(14)
        stats_box.append(stats_icon)
        stats_lbl = Gtk.Label(label=f"{_format_compact_count(int(hit.downloads or 0))} downloads", xalign=0.0)
        stats_lbl.add_css_class("dim-label")
        stats_box.append(stats_lbl)

        title_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_col.set_hexpand(True)
        title_col.append(title_lbl)
        title_col.append(author_lbl)
        title_col.append(stats_box)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        header_row.set_hexpand(True)
        header_row.append(icon)
        header_row.append(title_col)
        header_row.append(install_btn)
        content.append(header_row)

        if hit.categories:
            cats_box = Gtk.FlowBox()
            cats_box.set_max_children_per_line(8)
            cats_box.set_selection_mode(Gtk.SelectionMode.NONE)
            cats_box.set_column_spacing(6)
            cats_box.set_row_spacing(4)
            for cat in hit.categories:
                chip = Gtk.Label(label=cat)
                chip.add_css_class("mod-chip")
                chip.add_css_class("caption")
                cats_box.append(chip)
            content.append(cats_box)

        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.set_margin_top(4)
        separator.set_margin_bottom(4)
        content.append(separator)

        summary_lbl = Gtk.Label(label=(hit.summary or _("No description available.")).strip(), xalign=0.0)
        summary_lbl.set_wrap(True)
        summary_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        summary_lbl.set_hexpand(True)
        content.append(summary_lbl)

        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        version_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        version_title = Gtk.Label(label=_("Versions"), xalign=0.0)
        version_title.add_css_class("title-3")
        version_section.append(version_title)

        version_listbox = Gtk.ListBox()
        version_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        version_listbox.add_css_class("boxed-list")
        loading_row = Gtk.ListBoxRow()
        loading_row.set_activatable(False)
        loading_lbl = Gtk.Label(label=_("Loading versions…"), xalign=0.0, margin_top=10, margin_bottom=10)
        loading_lbl.add_css_class("dim-label")
        loading_row.set_child(loading_lbl)
        version_listbox.append(loading_row)
        version_section.append(version_listbox)
        content.append(version_section)

        open_btn = Gtk.Button(label=_("Open in CurseForge"))
        open_btn.add_css_class("pill")
        open_btn.set_halign(Gtk.Align.START)

        def on_open_page(*_args):
            url = hit.website_url or f"https://www.curseforge.com/minecraft/modpacks/{hit.slug or hit.mod_id}"
            if not _open_uri(url):
                self._alert(_("Could not open browser"), _("Unable to open the CurseForge page."))

        open_btn.connect("clicked", on_open_page)
        content.append(open_btn)

        sw = Gtk.ScrolledWindow()
        sw.add_css_class("mod-scroll")
        sw.set_vexpand(True)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        clamp = Adw.Clamp()
        clamp.set_maximum_size(900)
        clamp.set_child(content)
        sw.set_child(clamp)
        tv.set_content(sw)

        files: list = []
        selected_index = [0]

        def selected_file():
            if not files or not 0 <= selected_index[0] < len(files):
                return None
            return files[selected_index[0]]

        def update_checkmarks():
            index = 0
            while True:
                row = version_listbox.get_row_at_index(index)
                if row is None:
                    break
                box = row.get_child()
                if isinstance(box, Gtk.Box):
                    last = box.get_last_child()
                    if isinstance(last, Gtk.Image):
                        last.set_opacity(1.0 if index == selected_index[0] else 0.0)
                index += 1

        def on_version_activated(_listbox, row):
            index = getattr(row, "_file_index", None)
            if index is None:
                return
            selected_index[0] = index
            update_checkmarks()

        version_listbox.connect("row-activated", on_version_activated)

        def populate_versions(loaded, error):
            while True:
                row = version_listbox.get_row_at_index(0)
                if row is None:
                    break
                version_listbox.remove(row)

            if error or not loaded:
                row = Gtk.ListBoxRow()
                row.set_activatable(False)
                message = _("No versions for Minecraft {} on {}.").format(
                    self._server_info.mc_version if self._server_info else "?",
                    get_loader_display_name(self._server_loader()),
                )
                label = Gtk.Label(label=error or message, xalign=0.0, margin_top=10, margin_bottom=10)
                label.set_wrap(True)
                label.add_css_class("dim-label")
                row.set_child(label)
                version_listbox.append(row)
                set_detail_btn(sensitive=False)
                return

            files.extend(loaded)
            for index, entry in enumerate(loaded):
                row = Gtk.ListBoxRow()
                row.set_activatable(True)
                row._file_index = index

                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                box.set_margin_start(12)
                box.set_margin_end(12)
                box.set_margin_top(8)
                box.set_margin_bottom(8)

                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                col.set_hexpand(True)
                name = Gtk.Label(label=entry.display_name or entry.file_name, xalign=0.0)
                name.set_ellipsize(Pango.EllipsizeMode.END)
                col.append(name)

                bits = [entry.release_label]
                if entry.file_date:
                    bits.append(entry.file_date[:10])
                if entry.file_length:
                    bits.append(_format_size(entry.file_length))
                # A server pack is the author's own server build, so it is the one to want.
                bits.append(_("server pack") if entry.server_pack_file_id else _("client pack"))
                meta = Gtk.Label(label=" · ".join(bits), xalign=0.0)
                meta.add_css_class("caption")
                meta.add_css_class("dim-label")
                col.append(meta)
                box.append(col)

                check = Gtk.Image.new_from_icon_name("object-select-symbolic")
                check.set_opacity(1.0 if index == 0 else 0.0)
                box.append(check)

                row.set_child(box)
                version_listbox.append(row)

            if self._is_curseforge_pack_installed(hit.mod_id):
                set_detail_btn(_("Installed"), False)
            else:
                set_detail_btn(_("Install"), True)

        def load_versions():
            try:
                loaded = curseforge_client.get_files(
                    hit.mod_id,
                    game_version=self._server_info.mc_version if self._server_info else "",
                    loader=self._server_loader(),
                    page_size=30,
                )
                GLib.idle_add(lambda entries=loaded: populate_versions(entries, None) or False)
            except Exception as ex:
                GLib.idle_add(lambda message=str(ex): populate_versions([], message) or False)

        threading.Thread(target=load_versions, daemon=True).start()

        def on_install_clicked(*_args):
            self._start_curseforge_install(hit, selected_file(), set_detail_btn)

        install_btn.connect("clicked", on_install_clicked)

        return Adw.NavigationPage(title=hit.name, child=tv)

    # ------------------------------------------------------------------- install
    def _start_curseforge_install(self, hit, pack_file, set_btn) -> None:
        """Confirm, then install in the background. *pack_file* None means newest compatible."""
        from niksnaks_hosting.shared.backend import curseforge_client

        if self._is_running():
            self._alert(_("Server is running"), _("Stop the server before installing a modpack."))
            return
        if not self._server_info or not self._server_manager:
            self._alert(_("No server selected"), _("Select a server before installing a modpack."))
            return

        mc_version = self._server_info.mc_version
        loader = self._server_loader()

        def confirm(chosen):
            if not chosen:
                set_btn(_("Install"), True)
                self._alert(
                    _("No compatible version"),
                    _("{} has no release for Minecraft {} on {}.").format(
                        hit.name, mc_version, get_loader_display_name(loader)
                    ),
                )
                return

            body = _(
                "This installs {} into the server, replacing files it brings along.\n\n"
                "Your world, whitelist and server settings are kept."
            ).format(chosen.display_name or chosen.file_name)
            if chosen.file_length:
                body += "\n\n" + _("Download size: about {}.").format(_format_size(chosen.file_length))
            if not chosen.server_pack_file_id:
                body += "\n\n" + _(
                    "This pack has no server build, so its client mods are installed too. "
                    "Some may not run on a server."
                )

            dialog = Adw.AlertDialog()
            dialog.set_heading(_("Install {}?").format(hit.name))
            dialog.set_body(body)
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("install", _("Install"))
            dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("install")
            dialog.set_close_response("cancel")

            def on_response(_dialog, response):
                if response == "install":
                    self._run_curseforge_install(hit, chosen, set_btn)
                else:
                    set_btn(_("Install"), True)

            dialog.connect("response", on_response)
            dialog.present(self.get_root())

        if pack_file is not None:
            set_btn(_("Installing…"), False)
            confirm(pack_file)
            return

        set_btn(_("Checking…"), False)

        def resolve():
            try:
                chosen = curseforge_client.find_compatible_file(hit.mod_id, mc_version, loader)
            except Exception:
                chosen = None
            GLib.idle_add(lambda c=chosen: confirm(c) or False)

        threading.Thread(target=resolve, daemon=True).start()

    def _run_curseforge_install(self, hit, pack_file, set_btn) -> None:
        from niksnaks_hosting.shared.backend import curseforge_client

        op_token = self._begin_mod_operation()
        if not op_token:
            self._alert(_("No server selected"), _("Select a server before installing a modpack."))
            set_btn(_("Install"), True)
            return

        root = self._server_dir()
        if not root:
            self._end_mod_operation(op_token)
            set_btn(_("Install"), True)
            return

        set_btn(_("Installing…"), False)

        def on_done(result):
            set_btn(_("Installed"), False)
            self._record_curseforge_install(hit, pack_file, result)
            self._end_mod_operation(op_token)
            self._rebuild_lists()

            self._toast(_("Installed {} ({} mods)").format(hit.name, len(result.managed_mod_files)))

            if result.manual_downloads:
                names = "\n".join(f"- {name}" for name, _mod_id in result.manual_downloads[:8])
                extra = ""
                if len(result.manual_downloads) > 8:
                    extra = "\n" + _("- and {} more").format(len(result.manual_downloads) - 8)
                self._alert(
                    _("Some mods need a manual download"),
                    _(
                        "Their authors do not allow downloads outside CurseForge, so "
                        "these were skipped:\n\n{}{}\n\nAdd them to the mods folder yourself."
                    ).format(names, extra),
                )
                return

            server_loader_version = str(self._server_info.loader_version or "") if self._server_info else ""
            if (
                result.pack_loader_version
                and server_loader_version
                and result.pack_loader_version != server_loader_version
            ):
                self._toast(
                    _("This pack targets {} {}; the server runs {}").format(
                        (result.pack_loader or "").title() or get_loader_display_name(self._server_loader()),
                        result.pack_loader_version,
                        server_loader_version,
                    ),
                    timeout=8,
                )

        def on_error(message: str):
            set_btn(_("Install"), True)
            self._end_mod_operation(op_token)
            self._alert(_("Install failed"), message)

        def on_progress(fraction: float):
            set_btn(f"{int(fraction * 100)}%", False)

        def thread_fn():
            try:
                result = curseforge_client.install_modpack(
                    hit.mod_id,
                    pack_file.file_id,
                    root,
                    progress_callback=lambda fraction, _message: GLib.idle_add(
                        lambda f=fraction: on_progress(f) or False
                    ),
                )
                GLib.idle_add(lambda r=result: on_done(r) or False)
            except Exception as ex:
                GLib.idle_add(lambda message=str(ex): on_error(message) or False)

        threading.Thread(target=thread_fn, daemon=True).start()
