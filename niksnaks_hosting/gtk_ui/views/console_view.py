import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk, Pango

from niksnaks_hosting.shared.backend.server_process import ServerProcess

# A busy server prints for as long as it runs, and a Gtk.TextBuffer holding a whole
# day of output makes the console — and every frame the window draws — slow. Keep the
# most recent MAX_SCROLLBACK_LINES, and only trim once TRIM_SLACK lines have piled up
# past that, so a delete costs one pass per thousand lines instead of one per line.
MAX_SCROLLBACK_LINES = 5000
TRIM_SLACK_LINES = 1000

class ConsoleView(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._process = None
        self._output_handler_id = None
        self._auto_scroll = True
        self._scroll_pending = False
        self._lines_since_trim = 0

        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_vexpand(True)
        self._scrolled.set_hexpand(True)
        self._scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self._textview = Gtk.TextView()
        self._textview.set_editable(False)
        self._textview.set_cursor_visible(False)
        self._textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._textview.set_monospace(True)
        self._textview.set_top_margin(0)
        self._textview.set_bottom_margin(12)
        self._textview.set_left_margin(16)
        self._textview.set_right_margin(16)
        self._textview.add_css_class("console-view")

        self._buffer = self._textview.get_buffer()

        self._create_tags()

        self._scrolled.set_child(self._textview)
        self.append(self._scrolled)

        input_shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        input_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        input_bar.add_css_class("console-input-bar")
        input_bar.set_margin_start(8)
        input_bar.set_margin_end(8)
        input_bar.set_margin_bottom(8)
        input_bar.set_margin_top(4)

        self._entry = Gtk.Entry()
        self._entry.set_placeholder_text(_("Type a command..."))
        self._entry.set_hexpand(True)
        self._entry.add_css_class("console-input")
        self._entry.connect("activate", self._on_entry_activate)
        input_bar.append(self._entry)

        send_btn = Gtk.Button(icon_name="mail-send-symbolic")
        send_btn.set_tooltip_text(_("Send command"))
        send_btn.add_css_class("flat")
        send_btn.connect("clicked", self._on_send_clicked)
        input_bar.append(send_btn)

        input_shell.append(input_bar)
        self.append(input_shell)

    def _create_tags(self):
        self._tag_niksnaks_hosting = self._buffer.create_tag("niksnaks-hosting", foreground="#7c6bf0", weight=Pango.Weight.BOLD)
        self._tag_info = self._buffer.create_tag("info", foreground="#7aa2f7")
        self._tag_warn = self._buffer.create_tag("warn", foreground="#e0af68")
        self._tag_error = self._buffer.create_tag("error", foreground="#f7768e")
        self._tag_time = self._buffer.create_tag("time", foreground="#565f89")

    def set_process(self, process: ServerProcess):
        if self._process and self._output_handler_id:
            try:
                self._process.disconnect(self._output_handler_id)
            except Exception:
                pass

        self._process = process
        if process:
            self._output_handler_id = process.connect("output-received", self._on_output_received)

    def clear(self):
        self._buffer.set_text("")
        self._lines_since_trim = 0

    def append_text(self, text: str):
        end_iter = self._buffer.get_end_iter()

        tag = None
        if text.startswith("[Niksnaks-Hosting]"):
            tag = self._tag_niksnaks_hosting
        elif "WARN" in text:
            tag = self._tag_warn
        elif "ERROR" in text or "Exception" in text:
            tag = self._tag_error
        elif "INFO" in text:
            tag = self._tag_info

        if tag:
            self._buffer.insert_with_tags(end_iter, text, tag)
        else:
            self._buffer.insert(end_iter, text)

        self._trim_scrollback(text)

        # A server can emit a burst of lines in one go; one scroll after the burst puts
        # the view in the same place as one scroll per line, for a fraction of the work.
        if self._auto_scroll and not self._scroll_pending:
            self._scroll_pending = True
            GLib.idle_add(self._scroll_to_bottom)

    def _trim_scrollback(self, text: str):
        # Asking the buffer for its line count is the only cost on the common path, so do
        # it once per slack window. Count the newlines actually added rather than the
        # calls, so a chunk carrying many lines still moves the window along.
        self._lines_since_trim += max(1, text.count("\n"))
        if self._lines_since_trim < TRIM_SLACK_LINES:
            return
        self._lines_since_trim = 0

        line_count = self._buffer.get_line_count()
        if line_count <= MAX_SCROLLBACK_LINES + TRIM_SLACK_LINES:
            return

        found, cut = self._buffer.get_iter_at_line(line_count - MAX_SCROLLBACK_LINES)
        if not found:
            return
        self._buffer.delete(self._buffer.get_start_iter(), cut)

    def _scroll_to_bottom(self):
        self._scroll_pending = False
        end_iter = self._buffer.get_end_iter()
        self._textview.scroll_to_iter(end_iter, 0.0, True, 0.0, 1.0)
        return False

    def _on_output_received(self, process, text):
        self.append_text(text)

    def _on_entry_activate(self, entry):
        self._send_command()

    def _on_send_clicked(self, button):
        self._send_command()

    def _send_command(self):
        text = self._entry.get_text().strip()
        if not text:
            return

        if self._process:

            self.append_text(f"> {text}\n")
            self._process.send_command(text)
        else:
            self.append_text(_("[Niksnaks-Hosting] No server process connected\n"))

        self._entry.set_text("")
