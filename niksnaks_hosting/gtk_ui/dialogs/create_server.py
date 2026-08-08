import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk

from niksnaks_hosting.gtk_ui.loader_version_row import (
    build_loader_version_row,
    selected_loader_version,
    set_loader_version_message,
    set_loader_versions,
    set_loader_versions_loading,
)
from niksnaks_hosting.shared.backend.bedrock_manager import BedrockVersionOption
from niksnaks_hosting.shared.backend.download_manager import LoaderVersionOption
from niksnaks_hosting.shared.backend.server_manager import ServerManager
from niksnaks_hosting.shared.utils.constants import (
    BEDROCK_DEFAULT_SERVER_PROPERTIES,
    BEDROCK_GAMEMODES,
    DEFAULT_SERVER_PROPERTIES,
    DIFFICULTIES,
    EDITION_BEDROCK,
    EDITION_JAVA,
    GAMEMODES,
    LEVEL_TYPE_NAMES,
    LEVEL_TYPES,
    LOADER_FABRIC,
    LOADER_FORGE,
    MAX_RAM_MB,
    MAX_SERVER_PORT,
    MIN_RAM_MB,
    MIN_SERVER_PORT,
    SUPPORTED_EDITIONS,
    SUPPORTED_LOADERS,
    get_edition_display_name,
    get_loader_display_name,
    get_required_java_version,
    is_bedrock,
    normalize_loader,
)
from niksnaks_hosting.shared.utils.image_utils import convert_to_png

FABRIC_OPTIMISATION_MODS = [
    ("lithium", _("Lithium")),
    ("ferrite-core", _("FerriteCore")),
    ("c2me-fabric", _("Concurrent Chunk Management Engine")),
    ("fast-noise", _("Fast Noise")),
    ("vmp-fabric", _("Very Many Players")),
    ("scalablelux", _("ScalableLux")),
    ("krypton", _("Krypton")),
    ("modernfix", _("ModernFix")),
]

FORGE_OPTIMISATION_MODS = [
    ("ferrite-core", _("FerriteCore")),
    ("modernfix", _("ModernFix")),
    ("canary", _("Canary")),
    ("embeddium", _("Embeddium")),
    ("spark", _("spark")),
]

LOADER_LABELS = [get_loader_display_name(loader) for loader in SUPPORTED_LOADERS]

EDITION_LABELS = [get_edition_display_name(edition) for edition in SUPPORTED_EDITIONS]

DIFFICULTY_MODES = [*DIFFICULTIES, "hardcore"]

COMMON_JAVA_VERSIONS = [8, 11, 16, 17, 21, 25]

class CreateServerDialog(Adw.Dialog):
    __gsignals__ = {
        "server-created": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, server_manager: ServerManager):
        super().__init__()
        self._server_manager = server_manager
        self._game_versions: list[str] = []
        self._loader_options: list[LoaderVersionOption] = []
        self._bedrock_options: list[BedrockVersionOption] = []
        self._fetch_gen: int = 0
        self._loader_fetch_gen: int = 0
        self._loader_fetch_key: tuple[str, str] = ("", "")
        self._icon_source_path: str = ""
        self._world_import_source_path: str = ""
        # Once the user picks a port themselves, the edition's default stops overwriting it.
        self._port_edited: bool = False
        self._suppress_port_change: bool = False
        self._port_conflict_name: str | None = None
        # label, archive path, id of the server it came from ("" when browsed off disk)
        self._backup_choices: list[tuple[str, Path, str]] = []
        self._backup_edition: str = EDITION_JAVA

        self.set_title(_("Create Server"))
        self.set_content_width(500)
        self.set_content_height(600)

        self._toolbar_view = Adw.ToolbarView()

        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)

        self._cancel_btn = Gtk.Button(label=_("Cancel"))
        self._cancel_btn.connect("clicked", self._on_cancel_clicked)
        header.pack_start(self._cancel_btn)

        self._create_btn = Gtk.Button(label=_("Next"))
        self._create_btn.add_css_class("suggested-action")
        self._create_btn.set_sensitive(False)
        self._create_btn.connect("clicked", self._on_primary_clicked)
        header.pack_end(self._create_btn)

        self._toolbar_view.add_top_bar(header)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT)

        details_page = self._build_details_page()
        self._stack.add_named(details_page, "details")

        runtime_page = self._build_runtime_page()
        self._stack.add_named(runtime_page, "runtime")

        progress_page = self._build_progress_page()
        self._stack.add_named(progress_page, "progress")

        self._stack.connect("notify::visible-child-name", self._on_page_changed)

        self._toolbar_view.set_content(self._stack)
        self.set_child(self._toolbar_view)

        self._reload_backup_options()
        self._apply_edition_visibility()
        self._fetch_versions()

    def _build_details_page(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        page = Adw.PreferencesPage()

        info_group = Adw.PreferencesGroup(
            title=_("Server Info"),
        )

        self._name_entry = Adw.EntryRow(title=_("Server Name"))
        self._name_entry.set_text(_("My Server"))
        self._name_entry.connect("changed", self._validate)
        info_group.add(self._name_entry)

        self._icon_row = Adw.ActionRow(
            title=_("Server Icon"),
            subtitle=_("No icon selected"),
        )
        self._choose_icon_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self._choose_icon_btn.add_css_class("flat")
        self._choose_icon_btn.set_tooltip_text(_("Choose server icon"))
        self._choose_icon_btn.set_valign(Gtk.Align.CENTER)
        self._choose_icon_btn.set_child(Gtk.Image.new_from_icon_name("folder-symbolic"))

        self._choose_icon_btn.connect("clicked", self._on_choose_icon)
        self._icon_row.add_suffix(self._choose_icon_btn)
        self._icon_row.set_activatable_widget(self._choose_icon_btn)
        info_group.add(self._icon_row)

        page.add(info_group)

        self._backup_group = Adw.PreferencesGroup(
            title=_("Start from backup"),
            description=_("Set the new server up from a full backup instead of installing a fresh one."),
        )

        self._backup_row = Adw.ComboRow(
            title=_("Full backup"),
            model=Gtk.StringList.new([_("Don't use a backup")]),
        )
        self._backup_row.connect("notify::selected", self._on_backup_selected)
        self._backup_group.add(self._backup_row)

        self._backup_file_row = Adw.ActionRow(
            title=_("Choose a backup file"),
            subtitle=_("Pick a full backup .zip from anywhere on this computer"),
        )
        self._choose_backup_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self._choose_backup_btn.add_css_class("flat")
        self._choose_backup_btn.set_tooltip_text(_("Choose backup file"))
        self._choose_backup_btn.set_child(Gtk.Image.new_from_icon_name("folder-symbolic"))
        self._choose_backup_btn.connect("clicked", self._on_choose_backup_file)
        self._backup_file_row.add_suffix(self._choose_backup_btn)
        self._backup_file_row.set_activatable_widget(self._choose_backup_btn)
        self._backup_group.add(self._backup_file_row)

        page.add(self._backup_group)

        self._world_group = Adw.PreferencesGroup(
            title=_("World defaults"),
        )
        world_group = self._world_group

        self._difficulty_values = list(DIFFICULTY_MODES)
        difficulty_labels = [
            _("Hardcore") if value == "hardcore" else value.title() for value in self._difficulty_values
        ]
        self._difficulty_row = Adw.ComboRow(
            title=_("Difficulty"),
            model=Gtk.StringList.new(difficulty_labels),
        )
        default_is_hardcore = str(DEFAULT_SERVER_PROPERTIES.get("hardcore", "false")).lower() == "true"
        if default_is_hardcore and "hardcore" in self._difficulty_values:
            self._difficulty_row.set_selected(self._difficulty_values.index("hardcore"))
        else:
            default_difficulty = str(DEFAULT_SERVER_PROPERTIES.get("difficulty", "easy"))
            if default_difficulty in self._difficulty_values:
                self._difficulty_row.set_selected(self._difficulty_values.index(default_difficulty))
        world_group.add(self._difficulty_row)

        self._gamemode_values = list(GAMEMODES)
        gamemode_labels = [value.replace("-", " ").title() for value in self._gamemode_values]
        self._gamemode_row = Adw.ComboRow(
            title=_("Default gamemode"),
            model=Gtk.StringList.new(gamemode_labels),
        )
        default_gamemode = str(DEFAULT_SERVER_PROPERTIES.get("gamemode", "survival"))
        if default_gamemode in self._gamemode_values:
            self._gamemode_row.set_selected(self._gamemode_values.index(default_gamemode))
        world_group.add(self._gamemode_row)

        self._level_type_values = list(LEVEL_TYPES)
        level_type_labels = [LEVEL_TYPE_NAMES.get(value, value) for value in self._level_type_values]
        self._level_type_row = Adw.ComboRow(
            title=_("World type"),
            model=Gtk.StringList.new(level_type_labels),
        )
        default_level_type = str(DEFAULT_SERVER_PROPERTIES.get("level-type", "minecraft\\:normal"))
        if default_level_type in self._level_type_values:
            self._level_type_row.set_selected(self._level_type_values.index(default_level_type))
        world_group.add(self._level_type_row)

        self._seed_entry = Adw.EntryRow(title=_("World Seed"))
        self._seed_entry.set_text("")
        self._seed_entry.set_show_apply_button(False)
        world_group.add(self._seed_entry)

        self._world_import_row = Adw.ActionRow(
            title=_("Import world"),
            subtitle=_("No world selected."),
        )
        self._choose_world_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self._choose_world_btn.add_css_class("flat")
        self._choose_world_btn.set_tooltip_text(_("Choose world folder"))
        self._choose_world_btn.set_child(Gtk.Image.new_from_icon_name("folder-symbolic"))
        self._choose_world_btn.connect("clicked", self._on_choose_world_folder)
        self._world_import_row.add_suffix(self._choose_world_btn)

        self._choose_world_archive_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self._choose_world_archive_btn.add_css_class("flat")
        self._choose_world_archive_btn.set_tooltip_text(_("Choose world archive"))
        self._choose_world_archive_btn.set_child(Gtk.Image.new_from_icon_name("package-x-generic-symbolic"))
        self._choose_world_archive_btn.connect("clicked", self._on_choose_world_archive)
        self._world_import_row.add_suffix(self._choose_world_archive_btn)

        self._remove_world_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self._remove_world_btn.add_css_class("flat")
        self._remove_world_btn.set_tooltip_text(_("Remove imported world"))
        self._remove_world_btn.set_child(Gtk.Image.new_from_icon_name("window-close-symbolic"))
        self._remove_world_btn.connect("clicked", self._on_remove_world)
        self._remove_world_btn.set_visible(False)
        self._world_import_row.add_suffix(self._remove_world_btn)

        self._world_import_row.set_activatable_widget(self._choose_world_btn)
        world_group.add(self._world_import_row)

        page.add(world_group)

        scrolled.set_child(page)
        return scrolled

    def _build_runtime_page(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        page = Adw.PreferencesPage()

        self._version_group = Adw.PreferencesGroup(
            title=_("Runtime"),
        )
        version_group = self._version_group

        self._edition_row = Adw.ComboRow(
            title=_("Edition"),
            subtitle=_("Java Edition supports mods; Bedrock is the console and mobile edition"),
            model=Gtk.StringList.new(EDITION_LABELS),
        )
        self._edition_row.connect("notify::selected", self._on_edition_changed)
        version_group.add(self._edition_row)

        self._loader_row = Adw.ComboRow(
            title=_("Mod loader"),
            model=Gtk.StringList.new(LOADER_LABELS),
        )
        self._loader_row.set_tooltip_text(_("Fabric and Forge servers are supported"))
        self._loader_row.connect("notify::selected", self._on_loader_changed)
        version_group.add(self._loader_row)

        self._mc_version_row = Adw.ComboRow(
            title=_("Minecraft version"),
            model=Gtk.StringList.new([_("Loading...")]),
        )
        self._mc_version_row.set_sensitive(False)
        self._mc_version_row.connect("notify::selected", self._on_mc_version_changed)
        version_group.add(self._mc_version_row)

        self._loader_version_row = build_loader_version_row()
        version_group.add(self._loader_version_row)

        java_labels = [f"Java {v}" for v in COMMON_JAVA_VERSIONS]
        self._java_version_row = Adw.ComboRow(
            title=_("Java Version"),
            subtitle=_("Detecting..."),
            model=Gtk.StringList.new(java_labels),
        )
        self._java_version_row.connect("notify::selected", self._on_java_version_changed)
        version_group.add(self._java_version_row)

        page.add(version_group)

        self._resources_group = Adw.PreferencesGroup(
            title=_("Resources"),
        )
        resources_group = self._resources_group

        ram_adj = Gtk.Adjustment(
            value=self._server_manager.preferences.default_ram_mb,
            lower=MIN_RAM_MB,
            upper=MAX_RAM_MB,
            step_increment=256,
            page_increment=1024,
        )
        self._ram_row = Adw.SpinRow(
            title=_("RAM (MB)"),
            subtitle=_("Memory allocated to the server"),
            adjustment=ram_adj,
        )
        resources_group.add(self._ram_row)

        page.add(resources_group)

        self._network_group = Adw.PreferencesGroup(
            title=_("Network"),
        )

        port_adj = Gtk.Adjustment(
            value=self._server_manager.get_next_available_port(),
            lower=MIN_SERVER_PORT,
            upper=MAX_SERVER_PORT,
            step_increment=1,
            page_increment=10,
        )
        self._port_row = Adw.SpinRow(
            title=_("Port"),
            adjustment=port_adj,
        )
        self._port_row.connect("notify::value", self._on_port_changed)
        self._network_group.add(self._port_row)

        page.add(self._network_group)

        self._mods_group = Adw.PreferencesGroup(
            title=_("Optional setup"),
        )
        mods_group = self._mods_group
        self._optimise_row = Adw.SwitchRow(
            title=_("Install server-optimising mods"),
            subtitle=_("Installs compatible performance mods"),
        )
        self._optimise_row.set_active(False)
        mods_group.add(self._optimise_row)
        page.add(mods_group)

        scrolled.set_child(page)
        return scrolled

    def _build_progress_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_start(40)
        box.set_margin_end(40)

        self._progress_status = Adw.StatusPage()
        self._progress_status.set_icon_name("folder-download-symbolic")
        self._progress_status.set_title(_("Creating Server"))
        self._progress_status.set_description(_("Preparing..."))

        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_show_text(True)
        self._progress_bar.set_margin_start(40)
        self._progress_bar.set_margin_end(40)
        self._progress_bar.add_css_class("niksnaks-hosting-progress")

        progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        progress_box.append(self._progress_bar)

        self._progress_label = Gtk.Label(label="")
        self._progress_label.add_css_class("dim-label")
        progress_box.append(self._progress_label)

        self._progress_status.set_child(progress_box)

        box.append(self._progress_status)
        return box

    def _selected_loader(self) -> str:
        idx = self._loader_row.get_selected()
        if 0 <= idx < len(SUPPORTED_LOADERS):
            return SUPPORTED_LOADERS[idx]
        return LOADER_FABRIC

    def _selected_edition(self) -> str:
        idx = self._edition_row.get_selected()
        if 0 <= idx < len(SUPPORTED_EDITIONS):
            return SUPPORTED_EDITIONS[idx]
        return EDITION_JAVA

    def _is_bedrock(self) -> bool:
        return self._selected_edition() == EDITION_BEDROCK

    def _effective_edition(self) -> str:
        """The edition the new server will end up being.

        A backup carries its own edition, so the archive decides rather than the combo.
        """
        if self._using_backup():
            return self._backup_edition
        return self._selected_edition()

    def _selected_port(self) -> int:
        return int(self._port_row.get_value())

    def _port_conflict(self) -> str | None:
        """The conflict as of the last port or edition change.

        Answering this means reading every server's server.properties, and both the row
        and the Create button ask on the same change, so the answer is worked out once
        in _refresh_port_row and read from here.
        """
        return self._port_conflict_name

    def _on_port_changed(self, *_args) -> None:
        if self._suppress_port_change:
            return
        self._port_edited = True
        self._refresh_port_row()
        self._validate()

    def _refresh_port_defaults(self) -> None:
        """Move an untouched port onto the first one free for the chosen edition."""
        if not self._port_edited:
            self._suppress_port_change = True
            self._port_row.set_value(float(self._server_manager.get_next_available_port(self._effective_edition())))
            self._suppress_port_change = False
        self._refresh_port_row()

    def _refresh_port_row(self) -> None:
        port = self._selected_port()
        conflict = self._server_manager.find_port_conflict(port, self._effective_edition())
        self._port_conflict_name = conflict

        if conflict:
            self._port_row.add_css_class("error")
            self._port_row.set_subtitle(_('Already used by "{}". Pick another port.').format(conflict))
            return

        self._port_row.remove_css_class("error")
        if is_bedrock(self._effective_edition()):
            # Bedrock Dedicated Server takes the next port up for IPv6 as well.
            self._port_row.set_subtitle(_("Players connect on this port; {} is used for IPv6").format(port + 1))
        else:
            self._port_row.set_subtitle(_("Players connect on this port. Each server needs its own."))

    def _selected_loader_version(self) -> str:
        return selected_loader_version(self._loader_version_row, self._loader_options)

    def _on_edition_changed(self, *_args):
        self._game_versions = []
        self._loader_options = []
        self._bedrock_options = []
        self._fetch_gen += 1
        self._loader_fetch_gen += 1
        self._loader_fetch_key = ("", "")

        self._apply_edition_visibility()
        self._mc_version_row.set_model(Gtk.StringList.new([_("Loading...")]))
        set_loader_version_message(self._loader_version_row, _("Loading..."), _("Select a Minecraft version"))
        self._fetch_versions()
        self._validate()

    def _apply_edition_visibility(self) -> None:
        bedrock = self._is_bedrock()

        # Bedrock Dedicated Server is a native binary: no mod loader and no Java runtime.
        self._loader_row.set_visible(not bedrock)
        self._loader_version_row.set_visible(not bedrock)
        self._java_version_row.set_visible(not bedrock)
        self._mods_group.set_visible(not bedrock)

        # It has no heap either, so the same number becomes an OS-enforced ceiling. A backup
        # decides the edition itself, so the label follows the archive rather than the combo.
        limited = self._backup_edition == EDITION_BEDROCK if self._using_backup() else bedrock
        self._ram_row.set_title(_("Memory Limit (MB)") if limited else _("Allocated RAM (MB)"))
        self._ram_row.set_subtitle(
            _("Hard ceiling enforced by the system; the server stops if it is exceeded") if limited else ""
        )

        # Bedrock has no level-type, but it does import worlds — as .mcworld.
        self._level_type_row.set_visible(not bedrock)
        self._choose_world_btn.set_tooltip_text(_("Choose world folder"))
        self._choose_world_archive_btn.set_tooltip_text(
            _("Choose a .mcworld or .zip world") if bedrock else _("Choose a .zip world")
        )

        self._mc_version_row.set_title(_("Bedrock version") if bedrock else _("Minecraft version"))

        difficulties = list(DIFFICULTIES) if bedrock else list(DIFFICULTY_MODES)
        gamemodes = list(BEDROCK_GAMEMODES) if bedrock else list(GAMEMODES)
        self._set_world_default_options(difficulties, gamemodes)

        # Java and Bedrock start from different default ports, so the suggestion follows
        # whichever edition the new server will be.
        self._refresh_port_defaults()

        self._apply_backup_mode()

    def _apply_backup_mode(self) -> None:
        """A backup already carries its world, edition and loader, so those choices go away."""
        using_backup = self._using_backup()

        self._world_group.set_visible(not using_backup)
        self._version_group.set_visible(not using_backup)
        if using_backup:
            self._mods_group.set_visible(False)

    def _set_world_default_options(self, difficulties: list[str], gamemodes: list[str]) -> None:
        previous_difficulty = (
            self._difficulty_values[self._difficulty_row.get_selected()]
            if self._difficulty_row.get_selected() < len(self._difficulty_values)
            else ""
        )
        previous_gamemode = (
            self._gamemode_values[self._gamemode_row.get_selected()]
            if self._gamemode_row.get_selected() < len(self._gamemode_values)
            else ""
        )

        self._difficulty_values = difficulties
        labels = [_("Hardcore") if value == "hardcore" else value.title() for value in difficulties]
        self._difficulty_row.set_model(Gtk.StringList.new(labels))
        if previous_difficulty in difficulties:
            self._difficulty_row.set_selected(difficulties.index(previous_difficulty))
        elif "easy" in difficulties:
            self._difficulty_row.set_selected(difficulties.index("easy"))

        self._gamemode_values = gamemodes
        self._gamemode_row.set_model(Gtk.StringList.new([value.replace("-", " ").title() for value in gamemodes]))
        if previous_gamemode in gamemodes:
            self._gamemode_row.set_selected(gamemodes.index(previous_gamemode))
        elif "survival" in gamemodes:
            self._gamemode_row.set_selected(gamemodes.index("survival"))

    def _fetch_versions(self):
        self._mc_version_row.set_sensitive(False)
        self._fetch_gen += 1
        gen = self._fetch_gen

        if self._is_bedrock():

            def on_bedrock_versions(options):
                def apply():
                    if gen != self._fetch_gen:
                        return False
                    self._bedrock_options = options
                    self._game_versions = [option.version for option in options]
                    self._populate_versions()
                    return False

                GLib.idle_add(apply)

            self._server_manager.bedrock_manager.fetch_versions_async(on_bedrock_versions)
            return

        def on_versions(game_vers):
            def apply():
                if gen != self._fetch_gen:
                    return False
                self._game_versions = game_vers
                self._populate_versions()
                return False

            GLib.idle_add(apply)

        self._server_manager.download_manager.fetch_game_versions_for_loader_async(
            self._selected_loader(), on_versions
        )

    def _on_loader_changed(self, *_args):
        self._game_versions = []
        self._loader_options = []
        self._loader_fetch_gen += 1
        self._loader_fetch_key = ("", "")

        self._mc_version_row.set_model(Gtk.StringList.new([_("Loading...")]))
        set_loader_version_message(self._loader_version_row, _("Loading..."), _("Select a Minecraft version"))
        self._fetch_versions()
        self._validate()

    def _populate_versions(self):
        if self._game_versions:
            if self._is_bedrock():
                labels = [option.label for option in self._bedrock_options]
            else:
                labels = list(self._game_versions)
            self._mc_version_row.set_model(Gtk.StringList.new(labels))
            self._mc_version_row.set_sensitive(True)
            self._mc_version_row.set_selected(0)
            self._on_mc_version_changed(self._mc_version_row, None)
        else:
            self._mc_version_row.set_model(Gtk.StringList.new([_("No versions found")]))
            if not self._is_bedrock():
                set_loader_version_message(
                    self._loader_version_row, _("No versions found"), _("Select a Minecraft version")
                )

        self._validate()

    def _on_mc_version_changed(self, row, _pspec):
        idx = row.get_selected()
        if idx < len(self._game_versions):
            mc_ver = self._game_versions[idx]
            if not self._is_bedrock():
                java_ver = get_required_java_version(mc_ver)
                self._select_java_version(java_ver)
                self._load_loader_versions(mc_ver)

        self._validate()

    def _selected_bedrock_option(self) -> BedrockVersionOption | None:
        idx = self._mc_version_row.get_selected()
        if 0 <= idx < len(self._bedrock_options):
            return self._bedrock_options[idx]
        return None

    def _load_loader_versions(self, mc_version: str) -> None:
        loader = self._selected_loader()
        if self._loader_fetch_key == (loader, mc_version):
            return

        self._loader_fetch_key = (loader, mc_version)
        self._loader_options = []
        self._loader_fetch_gen += 1
        gen = self._loader_fetch_gen

        set_loader_versions_loading(self._loader_version_row, mc_version)

        def on_options(options):
            def done():
                if gen != self._loader_fetch_gen:
                    return False
                self._loader_options = options
                set_loader_versions(self._loader_version_row, options, loader, mc_version)
                self._validate()
                return False

            GLib.idle_add(done)

        self._server_manager.download_manager.fetch_compatible_loader_versions_async(loader, mc_version, on_options)

    def _select_java_version(self, java_ver: int) -> None:
        closest = min(COMMON_JAVA_VERSIONS, key=lambda v: abs(v - java_ver))
        self._java_version_row.set_selected(COMMON_JAVA_VERSIONS.index(closest))
        self._update_java_subtitle()

    def _on_java_version_changed(self, *_args) -> None:
        self._update_java_subtitle()

    def _update_java_subtitle(self) -> None:
        idx = self._java_version_row.get_selected()
        java_ver = COMMON_JAVA_VERSIONS[idx] if idx < len(COMMON_JAVA_VERSIONS) else 21
        java_mgr = self._server_manager.java_manager
        available = java_mgr.is_java_available(java_ver)
        system_ver = java_mgr.system_java_version

        if available:
            self._java_version_row.set_subtitle(_("Java {} ✓ Available").format(java_ver))
        elif system_ver and system_ver >= java_ver:
            self._java_version_row.set_subtitle(
                _("Java {} needed - system Java {} can be used").format(java_ver, system_ver)
            )
        else:
            self._java_version_row.set_subtitle(_("Java {} needed - will be downloaded automatically").format(java_ver))

    def _on_cancel_clicked(self, button):
        page = self._stack.get_visible_child_name()
        if page == "runtime":
            self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_RIGHT)
            self._stack.set_visible_child_name("details")
            self._validate()
            return
        self.close()

    def _on_choose_icon(self, *_args):
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Select Server Icon"))

        image_filter = Gtk.FileFilter()
        image_filter.set_name(_("Images"))
        image_filter.add_mime_type("image/png")
        image_filter.add_mime_type("image/jpeg")
        image_filter.add_mime_type("image/webp")
        image_filter.add_mime_type("image/bmp")

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(image_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(image_filter)

        dialog.open(self.get_root(), None, self._on_icon_file_chosen)

    def _on_icon_file_chosen(self, dialog, result):
        try:
            selected = dialog.open_finish(result)
            if not selected:
                return
            path = selected.get_path() or ""
            if not path:
                return
            self._icon_source_path = path
            self._icon_row.set_subtitle(Path(path).name)
        except GLib.Error:
            return

    def _world_archive_filters(self) -> Gio.ListStore:
        """File filters for the world archive formats the chosen edition understands."""
        store = Gio.ListStore.new(Gtk.FileFilter)

        if self._is_bedrock():
            mcworld = Gtk.FileFilter()
            mcworld.set_name(_("Bedrock world (.mcworld)"))
            mcworld.add_pattern("*.mcworld")
            store.append(mcworld)

        archive = Gtk.FileFilter()
        archive.set_name(_("Zip archive (.zip)"))
        archive.add_pattern("*.zip")
        store.append(archive)

        return store

    def _on_choose_world_folder(self, *_args):
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Import World Folder"))
        dialog.select_folder(self.get_root(), None, self._on_world_folder_chosen)

    def _on_choose_world_archive(self, *_args):
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Import World Archive"))
        dialog.set_filters(self._world_archive_filters())
        dialog.open(self.get_root(), None, self._on_world_archive_chosen)

    def _on_world_folder_chosen(self, dialog, result):
        try:
            selected = dialog.select_folder_finish(result)
        except GLib.Error:
            return

        raw_path = selected.get_path() if selected else ""
        if raw_path:
            self._accept_world_source(Path(raw_path))

    def _on_world_archive_chosen(self, dialog, result):
        try:
            selected = dialog.open_finish(result)
        except GLib.Error:
            return

        raw_path = selected.get_path() if selected else ""
        if raw_path:
            self._accept_world_source(Path(raw_path))

    def _accept_world_source(self, path: Path) -> None:
        self._world_import_source_path = str(path)
        message_parts = [path.name]

        # Only an unpacked Java world exposes its seed and type in a level.dat this app
        # can read; Bedrock's format and packed archives keep them out of reach until import.
        if not self._is_bedrock() and path.is_dir():
            from niksnaks_hosting.shared.utils.nbt_utils import get_world_info

            seed, wtype = get_world_info(path)
            if seed:
                self._seed_entry.set_text(seed)
                message_parts.append(_("Seed imported"))
            if wtype and wtype in self._level_type_values:
                self._level_type_row.set_selected(self._level_type_values.index(wtype))
                message_parts.append(_("Type imported"))

            if len(message_parts) == 1:
                message_parts.append(_("world type must match the selected World type"))

        self._world_import_row.set_subtitle(" · ".join(message_parts))

        self._choose_world_btn.set_visible(False)
        self._choose_world_archive_btn.set_visible(False)
        self._remove_world_btn.set_visible(True)
        self._world_import_row.set_activatable_widget(self._remove_world_btn)
        self._seed_entry.set_sensitive(False)
        self._level_type_row.set_sensitive(False)

    def _on_remove_world(self, *_args):
        self._world_import_source_path = ""
        self._world_import_row.set_subtitle(_("No world selected."))
        self._seed_entry.set_text("")
        self._seed_entry.set_sensitive(True)
        default_level_type = str(DEFAULT_SERVER_PROPERTIES.get("level-type", "minecraft\\:normal"))
        if default_level_type in self._level_type_values:
            self._level_type_row.set_selected(self._level_type_values.index(default_level_type))
        self._level_type_row.set_sensitive(True)
        self._choose_world_btn.set_visible(True)
        self._choose_world_archive_btn.set_visible(True)
        self._remove_world_btn.set_visible(False)
        self._world_import_row.set_activatable_widget(self._choose_world_btn)

    def _reload_backup_options(self) -> None:
        options = self._server_manager.list_full_backups()
        self._backup_choices = [(option.label, option.path, option.server_id) for option in options]
        self._set_backup_model(0)

    def _set_backup_model(self, selected: int) -> None:
        labels = [_("Don't use a backup")] + [choice[0] for choice in self._backup_choices]
        self._backup_row.set_model(Gtk.StringList.new(labels))
        self._backup_row.set_selected(selected)

    def _using_backup(self) -> bool:
        return self._backup_row.get_selected() > 0

    def _selected_backup(self) -> tuple[Path, str] | None:
        index = self._backup_row.get_selected() - 1
        if 0 <= index < len(self._backup_choices):
            choice = self._backup_choices[index]
            return choice[1], choice[2]
        return None

    def _on_backup_selected(self, *_args):
        chosen = self._selected_backup()
        if chosen:
            self._backup_edition = ServerManager.peek_backup_edition(chosen[0])
        self._apply_edition_visibility()
        self._validate()

    def _on_choose_backup_file(self, *_args):
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Choose a backup file"))

        filters = Gio.ListStore.new(Gtk.FileFilter)
        archive = Gtk.FileFilter()
        archive.set_name(_("Zip archive (.zip)"))
        archive.add_pattern("*.zip")
        filters.append(archive)
        dialog.set_filters(filters)

        dialog.open(self.get_root(), None, self._on_backup_file_chosen)

    def _on_backup_file_chosen(self, dialog, result):
        try:
            selected = dialog.open_finish(result)
        except GLib.Error:
            return

        raw_path = selected.get_path() if selected else ""
        if not raw_path:
            return

        # The picker's own entries come from iterdir(), so compare resolved paths rather
        # than however the file chooser happened to spell this one.
        path = Path(raw_path)
        resolved = path.resolve()
        index = next((i for i, choice in enumerate(self._backup_choices) if choice[1].resolve() == resolved), None)
        if index is None:
            self._backup_choices.append((path.name, path, ""))
            index = len(self._backup_choices) - 1

        self._set_backup_model(index + 1)

    def _on_page_changed(self, *_args):
        self._validate()

    def _validate(self, *args):
        name = self._name_entry.get_text().strip()
        if self._is_bedrock():
            has_versions = len(self._bedrock_options) > 0
        else:
            has_versions = len(self._game_versions) > 0 and len(self._loader_options) > 0
        page = self._stack.get_visible_child_name()

        if page == "details":
            self._cancel_btn.set_label(_("Cancel"))
            self._cancel_btn.set_sensitive(True)
            self._create_btn.set_label(_("Next"))
            self._create_btn.set_sensitive(bool(name))
            return

        if page == "runtime":
            self._cancel_btn.set_label(_("Back"))
            self._cancel_btn.set_sensitive(True)
            self._create_btn.set_label(_("Create"))
            # A backup brings its own server files, so no version has to be resolved first.
            ready = bool(name) and (self._using_backup() or has_versions)
            # Two servers on one port means the second one never starts, so creating on a
            # taken port is refused rather than left to fail later.
            self._create_btn.set_sensitive(ready and self._port_conflict() is None)
            return

        self._cancel_btn.set_label(_("Cancel"))
        self._cancel_btn.set_sensitive(False)
        self._create_btn.set_label(_("Create"))
        self._create_btn.set_sensitive(False)

    def _on_primary_clicked(self, button):
        page = self._stack.get_visible_child_name()
        if page == "details":
            self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT)
            self._stack.set_visible_child_name("runtime")
            self._validate()
            return

        if page == "runtime":
            pass
        else:
            return

        name = self._name_entry.get_text().strip()
        port = self._selected_port()

        if self._using_backup():
            chosen = self._selected_backup()
            if not name or not chosen:
                return

            self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT)
            self._stack.set_visible_child_name("progress")
            self._create_btn.set_sensitive(False)

            threading.Thread(
                target=self._install_from_backup_thread,
                args=(
                    name,
                    chosen[0],
                    chosen[1],
                    int(self._ram_row.get_value()),
                    self._icon_source_path,
                    port,
                ),
                daemon=True,
            ).start()
            return

        mc_idx = self._mc_version_row.get_selected()
        mc_version = self._game_versions[mc_idx] if mc_idx < len(self._game_versions) else ""

        seed = self._seed_entry.get_text().strip()
        difficulty_idx = self._difficulty_row.get_selected()
        difficulty = (
            self._difficulty_values[difficulty_idx]
            if difficulty_idx < len(self._difficulty_values)
            else str(DEFAULT_SERVER_PROPERTIES.get("difficulty", "easy"))
        )
        hardcore_mode = difficulty == "hardcore"
        difficulty_for_config = "hard" if hardcore_mode else difficulty
        gamemode_idx = self._gamemode_row.get_selected()
        gamemode = (
            self._gamemode_values[gamemode_idx]
            if gamemode_idx < len(self._gamemode_values)
            else str(DEFAULT_SERVER_PROPERTIES.get("gamemode", "survival"))
        )

        if self._is_bedrock():
            option = self._selected_bedrock_option()
            if not name or not option:
                return

            self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT)
            self._stack.set_visible_child_name("progress")
            self._create_btn.set_sensitive(False)

            threading.Thread(
                target=self._install_bedrock_thread,
                args=(
                    name,
                    option,
                    int(self._ram_row.get_value()),
                    seed,
                    difficulty_for_config,
                    gamemode,
                    self._icon_source_path,
                    self._world_import_source_path,
                    port,
                ),
                daemon=True,
            ).start()
            return

        loader_version = self._selected_loader_version()
        loader_type = self._selected_loader()
        ram_mb = int(self._ram_row.get_value())
        level_type_idx = self._level_type_row.get_selected()
        level_type = (
            self._level_type_values[level_type_idx]
            if level_type_idx < len(self._level_type_values)
            else str(DEFAULT_SERVER_PROPERTIES.get("level-type", "minecraft\\:normal"))
        )
        install_optimisations = bool(self._optimise_row.get_active())
        java_idx = self._java_version_row.get_selected()
        java_version = COMMON_JAVA_VERSIONS[java_idx] if java_idx < len(COMMON_JAVA_VERSIONS) else 21

        if not name or not mc_version or not loader_version:
            return

        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT)
        self._stack.set_visible_child_name("progress")
        self._create_btn.set_sensitive(False)

        thread = threading.Thread(
            target=self._install_thread,
            args=(
                name,
                mc_version,
                loader_version,
                loader_type,
                ram_mb,
                java_version,
                seed,
                difficulty_for_config,
                hardcore_mode,
                gamemode,
                level_type,
                self._icon_source_path,
                self._world_import_source_path,
                install_optimisations,
                port,
            ),
            daemon=True,
        )
        thread.start()

    def _install_from_backup_thread(self, name, archive, source_server_id, ram_mb, icon_source_path, port):

        try:
            self._update_progress(0.02, _("Restoring from backup..."), Path(archive).name)
            success, result, server_info = self._server_manager.create_server_from_backup(
                name,
                archive,
                ram_mb=ram_mb,
                source_server_id=source_server_id,
                progress_callback=lambda frac, text: self._update_progress(0.02 + frac * 0.90, text, ""),
                port=port,
            )
            if not success or not server_info:
                self._show_error(result)
                return

            if icon_source_path:
                self._update_progress(0.95, _("Applying server icon..."), "")
                try:
                    icon_output = server_info.server_dir / "icon.png"
                    convert_to_png(icon_source_path, str(icon_output), size=128)
                    self._server_manager.set_server_icon(server_info.id, str(icon_output))
                except Exception:
                    pass

            self._show_success(server_info.id)

        except Exception as e:
            self._show_error(_("Unexpected error: {}").format(e))

    def _install_bedrock_thread(
        self, name, option, ram_mb, seed, difficulty, gamemode, icon_source_path, world_import_source_path, port
    ):

        try:
            self._update_progress(0.05, _("Creating server..."), _("Bedrock {}").format(option.version))
            server_info = self._server_manager.add_server(
                name=name,
                mc_version=option.version,
                ram_mb=ram_mb,
                edition=EDITION_BEDROCK,
            )

            self._update_progress(0.10, _("Downloading Bedrock server..."), _("Bedrock {}").format(option.version))
            success, msg = self._server_manager.install_bedrock_server(
                server_info.id,
                option,
                progress_callback=lambda frac, text: self._update_progress(0.10 + frac * 0.78, text, ""),
            )
            if not success:
                self._show_error(msg)
                return

            self._update_progress(0.90, _("Applying server settings..."), "")
            from niksnaks_hosting.shared.backend.config_manager import ConfigManager

            config = ConfigManager(str(server_info.server_dir))
            config.load()
            for key, value in BEDROCK_DEFAULT_SERVER_PROPERTIES.items():
                config.set_value(key, value)
            config.set_value("server-name", name)
            config.set_value("difficulty", difficulty)
            config.set_value("gamemode", gamemode)
            config.set_value("level-seed", seed)
            # Bedrock listens on the chosen port for IPv4 and the next one up for IPv6.
            config.set_value("server-port", port)
            config.set_value("server-portv6", port + 1)
            config.save()

            # After the config, so the world lands under the level-name the server will load.
            if world_import_source_path:
                self._update_progress(0.93, _("Importing world..."), "")
                success, msg = self._server_manager.import_world_folder(
                    server_info.id,
                    world_import_source_path,
                )
                if not success:
                    self._show_error(_("Failed to import world: {}").format(msg))
                    return

            if icon_source_path:
                self._update_progress(0.95, _("Applying server icon..."), "")
                try:
                    icon_output = server_info.server_dir / "icon.png"
                    convert_to_png(icon_source_path, str(icon_output), size=128)
                    self._server_manager.set_server_icon(server_info.id, str(icon_output))
                except Exception:
                    pass

            self._show_success(server_info.id)

        except Exception as e:
            self._show_error(_("Unexpected error: {}").format(e))

    def _install_thread(
        self,
        name,
        mc_version,
        loader_version,
        loader_type,
        ram_mb,
        java_version,
        seed,
        difficulty,
        hardcore_mode,
        gamemode,
        level_type,
        icon_source_path,
        world_import_source_path,
        install_optimisations,
        port,
    ):

        try:
            java_ver = java_version
            java_mgr = self._server_manager.java_manager
            dl_mgr = self._server_manager.download_manager
            loader_type = normalize_loader(loader_type)

            if not java_mgr.is_java_available(java_ver):
                self._update_progress(
                    0.05, _("Downloading Java Runtime..."), _("JRE {} for MC {}").format(java_ver, mc_version)
                )

                success, msg = java_mgr.download_jre_sync(
                    java_ver,
                    progress_callback=lambda frac, msg: self._update_progress(
                        0.05 + frac * 0.20, msg, _("JRE {}").format(java_ver)
                    ),
                )

                if not success:
                    self._show_error(_("Failed to download JRE: {}").format(msg))
                    return

            java_path = java_mgr.get_java_path(java_ver)
            if not java_path:
                java_path = java_mgr.get_java_for_mc(mc_version) or "java"

            if loader_type == LOADER_FORGE:
                if not loader_version:
                    self._show_error(_("No Forge build available for Minecraft {}").format(mc_version))
                    return

                self._update_progress(0.28, _("Downloading Forge installer..."), "")
                from niksnaks_hosting.shared.utils.constants import get_forge_full_version

                full_version = get_forge_full_version(mc_version, loader_version)
                installer_path = dl_mgr.download_forge_installer(
                    full_version,
                    progress_callback=lambda frac, msg: self._update_progress(0.28 + frac * 0.14, msg, ""),
                )
                if not installer_path:
                    self._show_error(_("Failed to download Forge installer"))
                    return

                self._update_progress(0.44, _("Creating server..."), "")
                server_info = self._server_manager.add_server(
                    name=name,
                    mc_version=mc_version,
                    loader_version=loader_version,
                    ram_mb=ram_mb,
                    java_version=java_ver,
                    loader_type=loader_type,
                )

                self._update_progress(0.48, _("Installing Forge server..."), _("MC {}").format(mc_version))
                success, msg = dl_mgr.install_forge_server(
                    java_path=java_path,
                    installer_jar=installer_path,
                    mc_version=mc_version,
                    forge_build=loader_version,
                    server_dir=str(server_info.server_dir),
                    progress_callback=lambda frac, msg: self._update_progress(0.48 + frac * 0.38, msg, ""),
                )

                if not success:
                    self._show_error(_("Forge installation failed: {}").format(msg))
                    return

            else:
                self._update_progress(0.28, _("Downloading Fabric installer..."), "")

                installer_path = dl_mgr.download_installer(
                    progress_callback=lambda frac, msg: self._update_progress(0.28 + frac * 0.14, msg, ""),
                )

                if not installer_path:
                    self._show_error(_("Failed to download Fabric installer"))
                    return

                self._update_progress(0.44, _("Creating server..."), "")
                server_info = self._server_manager.add_server(
                    name=name,
                    mc_version=mc_version,
                    loader_version=loader_version,
                    ram_mb=ram_mb,
                    java_version=java_ver,
                    loader_type=loader_type,
                )

                self._update_progress(0.48, _("Downloading Minecraft server.jar..."), _("MC {}").format(mc_version))
                success, msg = dl_mgr.download_server_jar(
                    mc_version=mc_version,
                    server_dir=str(server_info.server_dir),
                    progress_callback=lambda frac, msg: self._update_progress(
                        0.48 + frac * 0.12, msg, _("MC {}").format(mc_version)
                    ),
                )

                if not success:
                    self._show_error(_("Failed to download server.jar: {}").format(msg))
                    return

                self._update_progress(0.62, _("Installing Fabric server..."), _("MC {}").format(mc_version))

                success, msg = dl_mgr.install_fabric_server(
                    java_path=java_path,
                    installer_jar=installer_path,
                    mc_version=mc_version,
                    server_dir=str(server_info.server_dir),
                    loader_version=loader_version if loader_version else None,
                    progress_callback=lambda frac, msg: self._update_progress(0.62 + frac * 0.24, msg, ""),
                )

                if not success:
                    self._show_error(_("Fabric installation failed: {}").format(msg))
                    return

            self._update_progress(0.88, _("Applying server settings..."), "")
            from niksnaks_hosting.shared.backend.config_manager import ConfigManager

            config = ConfigManager(str(server_info.server_dir))
            config.load()
            config.set_value("motd", DEFAULT_SERVER_PROPERTIES.get("motd", "a Niksnaks-Hosting server"))
            config.set_value("difficulty", difficulty)
            config.set_value("hardcore", bool(hardcore_mode))
            config.set_value("gamemode", gamemode)
            config.set_value("level-type", level_type)
            config.set_value("level-seed", seed)
            config.set_value("server-port", port)
            config.save()
            config.set_eula(True)

            if world_import_source_path:
                self._update_progress(0.90, _("Importing world..."), "")
                success, msg = self._server_manager.import_world_folder(
                    server_info.id,
                    world_import_source_path,
                )
                if not success:
                    self._show_error(_("Failed to import world: {}").format(msg))
                    return

            if icon_source_path:
                self._update_progress(0.92, _("Applying server icon..."), "")
                try:
                    icon_output = server_info.server_dir / "icon.png"
                    convert_to_png(icon_source_path, str(icon_output), size=128)
                    self._server_manager.set_server_icon(server_info.id, str(icon_output))
                except Exception:
                    pass

            if install_optimisations:
                self._update_progress(0.94, _("Installing server-optimising mods..."), "0/0")
                self._install_optimising_mods(server_info.server_dir, mc_version, loader_type)

            self._show_success(server_info.id)

        except Exception as e:
            self._show_error(_("Unexpected error: {}").format(e))

    def _install_optimising_mods(self, server_dir: Path, mc_version: str, loader_type: str) -> None:
        from niksnaks_hosting.shared.backend import modrinth_client

        loader_type = normalize_loader(loader_type)
        optimisation_mods = FORGE_OPTIMISATION_MODS if loader_type == LOADER_FORGE else FABRIC_OPTIMISATION_MODS

        mods_dir = Path(server_dir) / "mods"
        mods_dir.mkdir(parents=True, exist_ok=True)
        installed = {p.name.lower() for p in mods_dir.glob("*.jar")}

        total = len(optimisation_mods)
        done = 0
        for slug, title in optimisation_mods:
            done += 1
            progress = 0.94 + (done / max(1, total)) * 0.05
            self._update_progress(
                progress, _("Installing server-optimising mods..."), _("{} · {}").format(f"{done}/{total}", title)
            )
            try:
                version = self._find_supported_optimisation_version(
                    modrinth_client,
                    slug,
                    mc_version,
                    loader_type,
                )
                if not version:
                    continue
                if version.filename.lower() in installed:
                    continue
                modrinth_client.download_to(version.download_url, mods_dir / version.filename)
                installed.add(version.filename.lower())
                self._record_optimisation_mod_install(server_dir, version, title)
            except Exception:
                continue

    def _record_optimisation_mod_install(self, server_dir: Path, version, title: str) -> None:
        import json

        state_path = Path(server_dir) / ".niksnaks-hosting-mod-installs.json"
        try:
            if state_path.exists():
                with open(state_path, encoding="utf-8") as f:
                    state = json.load(f)
            else:
                state = {}
            mods = state.setdefault("mods", {})
            mods[version.project_id] = {
                "title": title,
                "version_id": version.version_id,
                "version_number": version.version_number,
                "filename": version.filename,
            }
            state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    def _find_supported_optimisation_version(self, modrinth_client, project_id: str, mc_version: str, loader_type: str):
        versions = modrinth_client.get_project_versions(project_id)
        if not versions:
            return None

        loader = normalize_loader(loader_type)
        for version in versions:
            has_mc = mc_version in (version.game_versions or [])
            has_loader = loader in [x.lower() for x in (version.loaders or [])]
            if has_mc and has_loader:
                return version
        return None

    def _update_progress(self, fraction, title, detail):
        def _update():
            self._progress_bar.set_fraction(min(1.0, fraction))
            self._progress_status.set_description(title)
            self._progress_label.set_label(detail)

        GLib.idle_add(_update)

    def _show_error(self, message):
        def _update():
            self._progress_status.set_icon_name("dialog-error-symbolic")
            self._progress_status.set_title(_("Creation Failed"))
            self._progress_status.set_description(message)
            self._progress_bar.set_fraction(0)
            self._progress_label.set_label(_("Please try again"))
            self._cancel_btn.set_sensitive(True)

        GLib.idle_add(_update)

    def _show_success(self, server_id):
        def _update():
            self._progress_status.set_icon_name("object-select-symbolic")
            self._progress_status.set_title(_("Server Created!"))
            self._progress_status.set_description(_("Your server is ready to start"))
            self._progress_bar.set_fraction(1.0)
            self._progress_label.set_label("")

            GLib.timeout_add(1500, lambda: self._finish(server_id))

        GLib.idle_add(_update)

    def _finish(self, server_id):
        self.emit("server-created", server_id)
        self.close()
        return False
