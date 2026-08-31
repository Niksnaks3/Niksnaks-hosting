import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk

from niksnaks_hosting.gtk_ui.host_form import (
    add_host_and_verify,
    build_host_form,
    connect_host_form_changed,
    host_draft,
    host_form_error,
    set_host_form_editable,
)
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
    LOADER_NEOFORGE,
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
    uses_installer,
)

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

# Canary is Lithium's Forge port and Embeddium only helps a client, so NeoForge servers
# take Lithium itself plus the tuning mods that publish a NeoForge build.
NEOFORGE_OPTIMISATION_MODS = [
    ("lithium", _("Lithium")),
    ("ferrite-core", _("FerriteCore")),
    ("modernfix", _("ModernFix")),
    ("scalablelux", _("ScalableLux")),
    ("noisium", _("Noisium")),
    ("spark", _("spark")),
]

OPTIMISATION_MODS_BY_LOADER = {
    LOADER_FABRIC: FABRIC_OPTIMISATION_MODS,
    LOADER_FORGE: FORGE_OPTIMISATION_MODS,
    LOADER_NEOFORGE: NEOFORGE_OPTIMISATION_MODS,
}

LOADER_LABELS = [get_loader_display_name(loader) for loader in SUPPORTED_LOADERS]

EDITION_LABELS = [get_edition_display_name(edition) for edition in SUPPORTED_EDITIONS]

DIFFICULTY_MODES = [*DIFFICULTIES, "hardcore"]

COMMON_JAVA_VERSIONS = [8, 11, 16, 17, 21, 25]

# Not a host id: the last entry of the "Runs on" list is a way in to the add-host page.
# Real host ids are hex (uuid4().hex[:12]) and this computer is "", so neither collides.
ADD_HOST_SENTINEL = "__add-host__"

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
        # Rebuilding the "Runs on" list re-fires notify::selected, which would otherwise
        # re-enter _on_host_changed and re-open the add-host page from its own success.
        self._suppress_host_change: bool = False
        # Where the picker goes back to when the add-host detour is cancelled.
        self._last_host_id: str = ""
        self._host_probe_running: bool = False
        # Bumped to disown an attempt the user has navigated away from; a connect can
        # take CONNECT_TIMEOUT (20s), which is long enough to start a second one.
        self._host_probe_gen: int = 0
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

        addhost_page = self._build_add_host_page()
        self._stack.add_named(addhost_page, "addhost")

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

        self._host_row = Adw.ComboRow(
            title=_("Runs on"),
            subtitle=_("This computer, or another one you connect to over SSH"),
        )
        self._host_ids: list[str] = []
        self._refresh_host_row()
        self._host_row.connect("notify::selected", self._on_host_changed)
        version_group.add(self._host_row)

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
        self._loader_row.set_tooltip_text(_("Fabric, Forge and NeoForge servers are supported"))
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
            value=self._server_manager.get_next_available_port(host_id=self._selected_host_id()),
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

    def _refresh_host_row(self, select_id: str | None = None) -> None:
        from niksnaks_hosting.shared.remote.hosts_manager import get_hosts_manager

        # Read the current choice before the id list is replaced under it.
        target = select_id if select_id is not None else self._selected_host_id()

        hosts = get_hosts_manager().all_hosts()
        self._host_ids = [host.id for host in hosts] + [ADD_HOST_SENTINEL]
        labels = [host.name for host in hosts] + [_("Add another computer…")]

        index = 0
        if target and target != ADD_HOST_SENTINEL and target in self._host_ids:
            index = self._host_ids.index(target)

        self._suppress_host_change = True
        try:
            self._host_row.set_model(Gtk.StringList.new(labels))
            self._host_row.set_selected(index)
        finally:
            self._suppress_host_change = False

    def _raw_selected_host_id(self) -> str:
        idx = self._host_row.get_selected()
        if 0 <= idx < len(self._host_ids):
            return self._host_ids[idx]
        return ""

    def _selected_host_id(self) -> str:
        # The sentinel is a way in to a page, never somewhere to put a server.
        host_id = self._raw_selected_host_id()
        return "" if host_id == ADD_HOST_SENTINEL else host_id

    def _on_host_changed(self, *_args) -> None:
        if self._suppress_host_change:
            return

        if self._raw_selected_host_id() == ADD_HOST_SENTINEL:
            # Replacing a combo row's model from inside its own notify::selected frees
            # the list it is still emitting from, which segfaults GTK. Hand the work to
            # the main loop so the emission has finished by the time it runs.
            GLib.idle_add(self._enter_add_host_page)
            return

        self._last_host_id = self._selected_host_id()
        self._apply_host_rules()
        # A port is only taken on one machine, so moving the server changes both the
        # suggestion and whether what is in the row is a clash at all.
        self._refresh_port_defaults()
        self._validate()

    def _enter_add_host_page(self) -> bool:
        # Put the picker back first, so backing out of the detour leaves no trace.
        self._refresh_host_row(select_id=self._last_host_id)
        self._open_add_host_page()
        return False

    def _apply_host_rules(self) -> None:
        host_id = self._selected_host_id()
        self._apply_world_import_rule(bool(host_id))
        self._update_java_subtitle()
        allowed, why_not = self._bedrock_allowed(host_id)
        if not allowed and self._edition_row.get_selected() != 0:
            self._edition_row.set_selected(0)
        self._edition_row.set_sensitive(allowed)
        self._edition_row.set_subtitle(
            why_not or _("Java Edition supports mods; Bedrock is the console and mobile edition")
        )

    def _bedrock_allowed(self, host_id: str) -> tuple[bool, str]:
        """Whether this host can run Bedrock, answered without connecting to ask.

        Mojang publishes Bedrock for 64-bit Intel and AMD only, on Windows and Linux. The
        platform and architecture were written down the first time the host was reached,
        so a host that has never been connected to gets the benefit of the doubt and is
        told at install time instead.
        """
        if not host_id:
            return True, ""

        from niksnaks_hosting.shared.remote.host_ops import make_ops
        from niksnaks_hosting.shared.remote.hosts_manager import get_hosts_manager

        hosts = get_hosts_manager()
        host = hosts.get(host_id)
        platform = hosts.platform_of(host_id)
        arch = getattr(host, "arch", "")
        if host is None or not platform or not arch:
            return True, ""
        if make_ops(platform).supports_bedrock(arch):
            return True, ""
        return False, _("{} is a {} machine, and Bedrock is only published for 64-bit Intel and AMD").format(
            host.name, arch
        )

    @staticmethod
    def _remote_platform_blocking(host_id: str) -> str:
        """Like _remote_platform, but connects to find out if it has to.

        Takes the host id rather than reading the picker, because this only ever runs
        on a worker thread -- a host that is slow to answer or unreachable must not
        freeze the wizard -- and the picker is a widget, which is not safe to touch
        from one. The caller reads it on the main thread and passes it in.
        """
        if not host_id:
            return ""
        from niksnaks_hosting.shared.remote.hosts_manager import get_hosts_manager

        hosts = get_hosts_manager()
        platform = hosts.platform_of(host_id)
        if platform:
            return platform
        host = hosts.get(host_id)
        if host is None:
            return ""
        try:
            return host.ops.platform
        except Exception:
            return ""

    # ------------------------------------------------------------ adding a computer
    def _build_add_host_page(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        heading = Gtk.Label(label=_("Add another computer"))
        heading.add_css_class("title-4")
        heading.set_xalign(0.0)
        box.append(heading)

        blurb = Gtk.Label(
            label=_(
                "Windows or Linux. The server is installed and run on that machine over "
                "SSH, keeps running there after you close this app, and Java is "
                "installed for you."
            )
        )
        blurb.add_css_class("dim-label")
        blurb.set_wrap(True)
        blurb.set_xalign(0.0)
        box.append(blurb)

        self._host_form = build_host_form()
        connect_host_form_changed(self._host_form, self._validate)
        box.append(self._host_form.box)

        self._host_status = Gtk.Label()
        self._host_status.add_css_class("caption")
        self._host_status.set_wrap(True)
        self._host_status.set_xalign(0.0)
        self._host_status.set_visible(False)
        box.append(self._host_status)

        scrolled.set_child(box)
        return scrolled

    def _open_add_host_page(self) -> None:
        self._set_host_status("", error=False)
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT)
        self._stack.set_visible_child_name("addhost")
        self._validate()

    def _set_host_status(self, text: str, error: bool) -> None:
        self._host_status.set_label(text)
        if error:
            self._host_status.add_css_class("error")
        else:
            self._host_status.remove_css_class("error")
        self._host_status.set_visible(bool(text))

    def _cancel_host_probe(self) -> None:
        """Stop caring about an attempt in flight; its thread finishes and is ignored."""
        self._host_probe_gen += 1
        self._host_probe_running = False
        set_host_form_editable(self._host_form, True)
        self._set_host_status("", error=False)

    def _start_host_probe(self) -> None:
        from niksnaks_hosting.shared.remote.hosts_manager import get_hosts_manager

        draft = host_draft(self._host_form)
        if draft is None:
            self._set_host_status(host_form_error(self._host_form), error=True)
            return

        self._host_probe_gen += 1
        generation = self._host_probe_gen
        self._host_probe_running = True
        set_host_form_editable(self._host_form, False)
        self._set_host_status(_("Connecting…"), error=False)
        self._validate()

        def still_wanted() -> bool:
            return generation == self._host_probe_gen

        def on_step(text: str) -> None:
            self._set_host_status(text, error=False)

        def on_done(host, detail: str) -> None:
            self._host_probe_running = False
            set_host_form_editable(self._host_form, True)

            if host is None:
                self._set_host_status(detail, error=True)
                self._validate()
                return

            # Straight back to where the wizard left off, with the new machine chosen.
            self._set_host_status("", error=False)
            self._last_host_id = host.id
            self._refresh_host_row(select_id=host.id)
            self._apply_host_rules()
            self._refresh_port_defaults()
            # The host works either way; a detail here means key login could not be set
            # up, which only matters once the app is closed. Said, not blocked on.
            self._host_row.set_subtitle(
                detail or _("This computer, or another one you connect to over SSH")
            )
            self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_RIGHT)
            self._stack.set_visible_child_name("runtime")
            self._validate()
            self._offer_adoption(host)

        add_host_and_verify(
            get_hosts_manager(),
            draft,
            on_step=on_step,
            on_done=on_done,
            still_wanted=still_wanted,
        )

    def _offer_adoption(self, host) -> None:
        """If this app is already installed over there, say so before making a new server.

        This is the moment the offer matches what the user is thinking: they have just
        pointed the app at a machine, and if that machine is already running servers,
        creating another one is probably not what they came for.
        """

        def worker() -> None:
            from niksnaks_hosting.shared.remote.remote_adopt import discover_remote_servers

            try:
                found, _where = discover_remote_servers(self._server_manager, host)
            except Exception:
                return
            waiting = [candidate for candidate in found if candidate.importable]
            if not waiting:
                return

            def ask() -> bool:
                dialog = Adw.AlertDialog()
                dialog.set_heading(_("{} is already running servers").format(host.name))
                dialog.set_body(
                    _(
                        "Niksnaks-Hosting is installed on {} and manages {} server(s) there. "
                        "You can control those from here instead of creating a new one."
                    ).format(host.name, len(waiting))
                )
                dialog.add_response("skip", _("Not now"))
                dialog.add_response("adopt", _("Show them"))
                dialog.set_response_appearance("adopt", Adw.ResponseAppearance.SUGGESTED)
                dialog.set_default_response("adopt")
                dialog.set_close_response("skip")

                def on_response(_d, response):
                    if response != "adopt":
                        return
                    from niksnaks_hosting.gtk_ui.dialogs.adopt_servers import show_adopt_dialog

                    show_adopt_dialog(self, self._server_manager, host)

                dialog.connect("response", on_response)
                dialog.present(self)
                return False

            GLib.idle_add(ask)

        threading.Thread(target=worker, daemon=True).start()

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
            self._port_row.set_value(
                float(
                    self._server_manager.get_next_available_port(
                        self._effective_edition(), host_id=self._selected_host_id()
                    )
                )
            )
            self._suppress_port_change = False
        self._refresh_port_row()

    def _refresh_port_row(self) -> None:
        port = self._selected_port()
        conflict = self._server_manager.find_port_conflict(
            port, self._effective_edition(), host_id=self._selected_host_id()
        )
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

            # A server bound for a Linux host needs the Linux zip, whichever kind of
            # machine this app happens to be running on. A host that has never been
            # connected to has to be connected to now to find that out -- off this
            # thread, since it may mean a live round trip to the far machine. The
            # picker is read here, on the main thread, and never inside the worker.
            host_id = self._selected_host_id()

            def worker() -> None:
                platform = self._remote_platform_blocking(host_id)
                on_bedrock_versions(self._server_manager.bedrock_manager.fetch_versions(platform))

            threading.Thread(target=worker, daemon=True).start()
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
        if self._selected_host_id():
            # What this computer has says nothing about the one that will run it, and
            # the install puts a Java on that machine itself if it needs to.
            self._java_version_row.set_subtitle(
                _("Java {} is installed on that computer if it has none").format(java_ver)
            )
            return
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
        if page == "addhost":
            self._cancel_host_probe()
            self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_RIGHT)
            self._stack.set_visible_child_name("runtime")
            self._validate()
            return
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

    def _apply_world_import_rule(self, remote: bool) -> None:
        """A world comes off this disk, and only a server on this disk can take it.

        The import copies the folder in with shutil, which has no idea what a path on
        another machine is. Left enabled, a world picked here would simply be dropped on
        the way to the host without anybody being told.
        """
        if remote and self._world_import_source_path:
            self._on_remove_world()
        self._world_import_row.set_sensitive(not remote)
        if remote:
            self._world_import_row.set_subtitle(
                _("Only a server on this computer can start from a world you already have")
            )
        elif not self._world_import_source_path:
            self._world_import_row.set_subtitle(_("No world selected."))

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
        self._apply_backup_host_rule()
        self._validate()

    def _apply_backup_host_rule(self) -> None:
        """A server made from a backup is made on this computer, so say so up front.

        create_server_from_backup reads the archive and writes the folder with plain
        local file calls; there is no path that puts one on a host. Leaving "Runs on"
        free would let the wizard promise a machine it then ignores.
        """
        using = self._using_backup()
        if using and self._selected_host_id():
            # Index 0 is always this computer -- all_hosts() lists it first.
            self._host_row.set_selected(0)
        self._host_row.set_sensitive(not using)
        self._host_row.set_subtitle(
            _("A server restored from a backup is always made on this computer")
            if using
            else _("This computer, or another one you connect to over SSH")
        )

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

        if page == "addhost":
            # Back stays live during a probe -- a wrong address otherwise strands the
            # user for the full 20s connect timeout. The generation counter is what
            # makes leaving safe; the disabled Connect is what stops a second attempt.
            self._cancel_btn.set_label(_("Back"))
            self._cancel_btn.set_sensitive(True)
            self._create_btn.set_label(_("Connect"))
            self._create_btn.set_sensitive(
                not self._host_probe_running and host_draft(self._host_form) is not None
            )
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

        if page == "addhost":
            self._start_host_probe()
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
                self._selected_host_id(),
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

            self._apply_icon(server_info.id, icon_source_path)

            self._show_success(server_info.id)

        except Exception as e:
            self._show_error(_("Unexpected error: {}").format(e))

    def _install_bedrock_thread(
        self, name, option, ram_mb, seed, difficulty, gamemode, icon_source_path, world_import_source_path, port
    ):

        if self._selected_host_id():
            self._install_remote_bedrock_thread(
                name, option, ram_mb, seed, difficulty, gamemode, port, icon_source_path
            )
            return

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

            self._apply_icon(server_info.id, icon_source_path)

            self._show_success(server_info.id)

        except Exception as e:
            self._show_error(_("Unexpected error: {}").format(e))

    def _install_remote_bedrock_thread(
        self, name, option, ram_mb, seed, difficulty, gamemode, port, icon_source_path=""
    ):
        """A Bedrock server on another machine: one zip, no Java, no loader.

        The world import the local path offers is left out: it copies a folder off this
        disk with shutil, which cannot write to the host. The icon can go, because the
        manager writes it through the server's own folder wherever that folder is.
        """
        from niksnaks_hosting.shared.backend.config_manager import ConfigManager
        from niksnaks_hosting.shared.remote.hosts_manager import get_hosts_manager
        from niksnaks_hosting.shared.remote.remote_install import install_remote_bedrock

        host_id = self._selected_host_id()
        host = get_hosts_manager().get(host_id)
        if host is None or host.is_local:
            self._show_error(_("That host is no longer set up in this app."))
            return

        self._update_progress(0.04, _("Checking {}…").format(host.name), "")
        try:
            ready, detail = host.check_ready()
        except Exception as e:
            ready, detail = False, str(e)
        if not ready:
            self._show_error(detail)
            return

        if not host.ops.supports_bedrock(host.arch or ""):
            self._show_error(
                _("{} is a {} machine, and Bedrock is only published for 64-bit Intel and AMD.").format(
                    host.name, host.arch or _("unrecognised")
                )
            )
            return

        keeps_running, why_not = host.ops.ensure_service_manager(host)
        if not keeps_running:
            self._show_error(why_not)
            return

        self._update_progress(0.10, _("Creating server on {}…").format(host.name), "")
        try:
            server_info = self._server_manager.add_server(
                name=name,
                mc_version=option.version,
                ram_mb=ram_mb,
                edition=EDITION_BEDROCK,
                host_id=host_id,
            )
        except Exception as e:
            self._show_error(_("Could not create the server on {}: {}").format(host.name, e))
            return

        try:
            install_remote_bedrock(
                host=host,
                server_dir=str(server_info.server_dir),
                option=option,
                progress=lambda f, text: self._update_progress(0.12 + f * 0.76, text, ""),
            )
        except Exception as e:
            # add_server() above already registered this server; a failed install must
            # not leave that record behind pointing at a folder with nothing usable in
            # it, or the sidebar shows a server that can never actually start.
            self._server_manager.delete_server(server_info.id)
            self._show_error(str(e))
            return

        self._update_progress(0.90, _("Applying server settings…"), "")
        try:
            config = ConfigManager(server_info.server_dir)
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
        except Exception as e:
            self._show_error(_("The server was installed but its settings could not be written: {}").format(e))
            return

        self._apply_icon(server_info.id, icon_source_path)
        self._show_success(server_info.id)

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
        host_id="",
    ):

        try:
            if host_id:
                self._install_remote_thread(
                    host_id, name, mc_version, loader_version, loader_type, ram_mb, java_version,
                    seed, difficulty, hardcore_mode, gamemode, level_type, install_optimisations, port,
                    icon_source_path,
                )
                return

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

            if uses_installer(loader_type):
                loader_name = get_loader_display_name(loader_type)

                if not loader_version:
                    self._show_error(
                        _("No {} build available for Minecraft {}").format(loader_name, mc_version)
                    )
                    return

                self._update_progress(0.28, _("Downloading {} installer...").format(loader_name), "")
                if loader_type == LOADER_NEOFORGE:
                    installer_path = dl_mgr.download_neoforge_installer(
                        loader_version,
                        progress_callback=lambda frac, msg: self._update_progress(0.28 + frac * 0.14, msg, ""),
                    )
                else:
                    from niksnaks_hosting.shared.utils.constants import get_forge_full_version

                    installer_path = dl_mgr.download_forge_installer(
                        get_forge_full_version(mc_version, loader_version),
                        progress_callback=lambda frac, msg: self._update_progress(0.28 + frac * 0.14, msg, ""),
                    )
                if not installer_path:
                    self._show_error(_("Failed to download {} installer").format(loader_name))
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

                self._update_progress(
                    0.48, _("Installing {} server...").format(loader_name), _("MC {}").format(mc_version)
                )
                if loader_type == LOADER_NEOFORGE:
                    success, msg = dl_mgr.install_neoforge_server(
                        java_path=java_path,
                        installer_jar=installer_path,
                        mc_version=mc_version,
                        server_dir=str(server_info.server_dir),
                        progress_callback=lambda frac, msg: self._update_progress(0.48 + frac * 0.38, msg, ""),
                    )
                else:
                    success, msg = dl_mgr.install_forge_server(
                        java_path=java_path,
                        installer_jar=installer_path,
                        mc_version=mc_version,
                        forge_build=loader_version,
                        server_dir=str(server_info.server_dir),
                        progress_callback=lambda frac, msg: self._update_progress(0.48 + frac * 0.38, msg, ""),
                    )

                if not success:
                    self._show_error(_("{} installation failed: {}").format(loader_name, msg))
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

            if install_optimisations:
                self._update_progress(0.94, _("Installing server-optimising mods..."), "0/0")
                self._install_optimising_mods(server_info.server_dir, mc_version, loader_type)

            self._apply_icon(server_info.id, icon_source_path)
            self._show_success(server_info.id)

        except Exception as e:
            self._show_error(_("Unexpected error: {}").format(e))

    def _install_remote_thread(
        self,
        host_id,
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
        install_optimisations,
        port,
        icon_source_path="",
    ):
        """Build the server on the host: its runtime, its loader and its settings."""
        from niksnaks_hosting.shared.backend.config_manager import ConfigManager
        from niksnaks_hosting.shared.remote.hosts_manager import get_hosts_manager
        from niksnaks_hosting.shared.remote.remote_install import install_remote_server

        hosts = get_hosts_manager()
        host = hosts.get(host_id)
        if host is None:
            self._show_error(_("That host is no longer set up in this app."))
            return

        loader_type = normalize_loader(loader_type)
        java_ver = java_version or get_required_java_version(mc_version)

        self._update_progress(0.04, _("Checking {}…").format(host.name), "")
        try:
            ready, detail = host.check_ready()
        except Exception as e:
            ready, detail = False, str(e)
        if not ready:
            self._show_error(detail)
            return

        # Whatever it takes for a server to outlive this app on that machine: lingering
        # for a systemd user session, a working Task Scheduler on Windows.
        keeps_running, why_not = host.ops.ensure_service_manager(host)
        if not keeps_running:
            self._show_error(why_not)
            return

        self._update_progress(0.10, _("Creating server on {}…").format(host.name), "")
        try:
            server_info = self._server_manager.add_server(
                name=name,
                mc_version=mc_version,
                loader_version=loader_version,
                ram_mb=ram_mb,
                java_version=java_ver,
                loader_type=loader_type,
                host_id=host_id,
            )
        except Exception as e:
            self._show_error(_("Could not create the server on {}: {}").format(host.name, e))
            return

        try:
            install_remote_server(
                host=host,
                server_dir=str(server_info.server_dir),
                mc_version=mc_version,
                loader_type=loader_type,
                loader_version=loader_version,
                java_version=java_ver,
                progress=lambda f, text: self._update_progress(0.12 + f * 0.74, text, ""),
            )
        except Exception as e:
            # add_server() above already registered this server; a failed install must
            # not leave that record behind pointing at a folder with nothing usable in
            # it, or the sidebar shows a server that can never actually start.
            self._server_manager.delete_server(server_info.id)
            self._show_error(str(e))
            return

        self._update_progress(0.88, _("Applying server settings…"), "")
        try:
            config = ConfigManager(server_info.server_dir)
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
        except Exception as e:
            self._show_error(_("The server was installed but its settings could not be written: {}").format(e))
            return

        if install_optimisations:
            self._update_progress(0.94, _("Installing server-optimising mods…"), "0/0")
            self._install_optimising_mods(server_info.server_dir, mc_version, loader_type, host=host)

        self._apply_icon(server_info.id, icon_source_path)
        self._show_success(server_info.id)

    def _apply_icon(self, server_id: str, icon_source_path: str) -> None:
        """The manager writes it into the server's own folder, on whichever machine that is."""
        if not icon_source_path:
            return
        self._update_progress(0.96, _("Applying server icon..."), "")
        try:
            self._server_manager.set_server_icon(server_id, icon_source_path)
        except Exception:
            pass

    def _install_optimising_mods(self, server_dir: Path, mc_version: str, loader_type: str, host=None) -> None:
        from niksnaks_hosting.shared.backend import modrinth_client

        loader_type = normalize_loader(loader_type)
        optimisation_mods = OPTIMISATION_MODS_BY_LOADER[loader_type]

        mods_dir = (server_dir if host is not None else Path(server_dir)) / "mods"
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
                if host is not None:
                    # The host pulls the jar itself; it never passes through this machine.
                    host.fetch_url(version.download_url, str(mods_dir / version.filename))
                else:
                    modrinth_client.download_to(version.download_url, mods_dir / version.filename)
                installed.add(version.filename.lower())
                self._record_optimisation_mod_install(server_dir, version, title)
            except Exception:
                continue

    def _record_optimisation_mod_install(self, server_dir: Path, version, title: str) -> None:
        import json

        from niksnaks_hosting.shared.remote.paths import is_remote

        root = server_dir if is_remote(server_dir) else Path(server_dir)
        state_path = root / ".niksnaks-hosting-mod-installs.json"
        try:
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
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
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
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
