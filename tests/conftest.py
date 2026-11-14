"""Stands in for the `orca` module and for a PrusaLink printer.

The stand-ins hold the rules that real testing found, because a stand-in that accepts anything would pass a
plugin that OrcaSlicer rejects. A slot's selector offers only presets that are shown and compatible, or the
slot's own, a selected preset's name finds the edited copy, and the printer signs in as the firmware does.
"""
import enum
import hashlib
import importlib
import json
import re
import secrets
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
MODULE = "orca_prusalink_filament_match_any"


class PluginResult(enum.Enum):
    Success = 0
    Skipped = 1


class ExecutionResult:
    def __init__(self, status, message):
        self.status = status
        self.message = message

    @staticmethod
    def success(message=""):
        return ExecutionResult(PluginResult.Success, message)

    @staticmethod
    def skipped(message=""):
        return ExecutionResult(PluginResult.Skipped, message)


class NotificationLevel(enum.Enum):
    RegularNotificationLevel = 0
    WarningNotificationLevel = 1


def serialized(value):
    """Format a value the way Preset.config_value() returns it: a list of strings is quoted where it needs it."""
    return f'"{value}"' if " " in value or "\n" in value else value


class FakePreset:
    def __init__(self, name, values=None, alias="", is_system=False, is_visible=True, is_compatible=True,
                 is_default=False, is_dirty=False):
        self.name = name
        self.alias = alias
        self.is_system = is_system
        self.is_visible = is_visible
        self.is_compatible = is_compatible
        self.is_default = is_default
        self.is_dirty = is_dirty
        self.values = dict(values or {})

    def config_value(self, key):
        return self.values.get(key)

    def label(self):
        return ("* " if self.is_dirty else "") + (self.alias or self.name)


def preset(name, vendor, material, color=None, hrc=None, parent=None, **flags):
    """A filament preset. A list of strings serializes quoted where needed, and inherits, a single string, never."""
    values = {"filament_vendor": serialized(vendor), "filament_type": serialized(material)}
    if color is not None:
        values["default_filament_colour"] = color
    if hrc is not None:
        values["required_nozzle_HRC"] = hrc
    if parent is not None:
        values["inherits"] = parent
    return FakePreset(name, values, **flags)


class FakeFilaments:
    """PresetCollection: the default preset, then the vendors' presets, then the user's, in order."""

    def __init__(self):
        default = FakePreset("Default Filament", {"filament_type": "PLA"}, is_default=True, is_visible=False)
        system = preset("Generic PLA @System", "Generic", "PLA", alias="Generic PLA", is_system=True)
        self.presets = {default.name: default, system.name: system}
        self.selected = system
        self.edited = system

    def add(self, *presets):
        for item in presets:
            self.presets[item.name] = item

    def size(self):
        return len(self.presets)

    def preset(self, index):
        return list(self.presets.values())[index]

    def find_preset(self, name):
        # The host's lookup hands back the edited copy for the selected preset.
        return self.edited if name == self.selected.name else self.presets.get(name)

    def select(self, name):
        self.selected = self.edited = self.presets[name]


class FakeFilamentEditor:
    """orca.host.preset.filament, which selects a preset for a 1-based slot through the slot's selector."""

    def __init__(self, filaments, slots):
        self.filaments = filaments
        self.slots = slots
        self.selections = []
        # Slots whose switch the user cancels, at the prompt about unsaved edits.
        self.cancelled_slots = set()
        self.mixed_color_slots = set()

    def begin_update(self, slot_number, preset_name=""):
        if not self.slots:
            # The project's one slot, which holds the selected preset.
            self.slots.append(self.filaments.selected.name)
        if not 1 <= slot_number <= len(self.slots):
            raise ValueError(f"Filament slot {slot_number} is out of range")
        if slot_number in self.mixed_color_slots:
            raise ValueError(f"Filament slot {slot_number} is a mixed-color virtual slot")
        target = self.filaments.presets.get(preset_name)
        offered = target is not None and target.is_visible and target.is_compatible
        if not (offered or preset_name == self.slots[slot_number - 1]):
            raise ValueError(f'Filament preset "{preset_name}" is not offered by the filament preset selector '
                             f"for filament slot {slot_number}")
        if slot_number in self.cancelled_slots:
            raise RuntimeError(f'Switching the edited filament preset to "{preset_name}" was canceled')
        self.filaments.select(preset_name)
        self.slots[slot_number - 1] = preset_name
        self.selections.append((slot_number, preset_name))
        return SimpleNamespace(commit=lambda: None)


class FakePrinter:
    """PrusaLink on Buddy firmware with Filament Slots, which takes an API key or RFC 2069 Digest.

    With `stock` set, it is stock firmware: no /api/v1/tools, and /api/printer names the active tool's material.
    """

    REALM = "Printer API"

    def __init__(self):
        self.tools = {}
        self.api_key = "mockKeyTest2345"
        self.user = "maker"
        self.password = "mockKeyTest2345"
        self.status = None
        self.body = None
        self.drop = False
        self.stock = False
        self.material = "PLA"
        self.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.address = f"127.0.0.1:{self.server.server_address[1]}"

    def tool(self, material="", vendor="", color="", color_hex="", nozzle=0.4, hardened=False):
        return {"loaded": bool(material), "material": material, "vendor": vendor, "color": color,
                "color_hex": color_hex, "nozzle_diameter": nozzle, "hardened": hardened, "high_flow": True}

    def authorized(self, headers):
        if headers.get("X-Api-Key") == self.api_key:
            return True
        fields = dict(re.findall(r'(\w+)="([^"]*)"', headers.get("Authorization", "")))
        if not fields or fields.get("username") != self.user or fields.get("realm") != self.REALM:
            return False
        first = hashlib.md5(f"{self.user}:{self.REALM}:{self.password}".encode()).hexdigest()
        second = hashlib.md5(f"GET:{fields['uri']}".encode()).hexdigest()
        return fields.get("response") == hashlib.md5(f"{first}:{fields['nonce']}:{second}".encode()).hexdigest()

    def handler(self):
        printer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data, headers=()):
                self.send_response(status)
                for header in headers:
                    self.send_header(*header)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                printer.requests.append((self.path, dict(self.headers)))
                if not printer.authorized(self.headers):
                    challenge = f'Digest realm="{printer.REALM}", nonce="{secrets.token_hex(8)}", stale=FALSE'
                    self.reply(401, b"Unauthorized", [("WWW-Authenticate", challenge)])
                elif self.path not in ("/api/printer", "/api/v1/tools") or (printer.stock and "v1" in self.path):
                    self.reply(404, b"Not Found")
                elif printer.drop:
                    return  # closes the connection without a reply
                elif printer.status:
                    self.reply(printer.status, b"Error")
                elif printer.body is not None:
                    self.reply(200, printer.body)
                elif self.path == "/api/printer":
                    # Abridged: stock firmware also reports temperatures and state here.
                    self.reply(200, json.dumps({"telemetry": {"temp-bed": 60.0, "material": printer.material}}).encode())
                else:
                    self.reply(200, json.dumps({"tools": printer.tools}).encode())

        return Handler


def fake_orca():
    module = types.ModuleType("orca")
    module.ExecutionResult = ExecutionResult
    module.script = types.SimpleNamespace(ScriptPluginCapabilityBase=type("ScriptBase", (), {
        "get_config": lambda self: json.dumps(getattr(self, "config", {})),
    }))
    module.base = object
    module.plugin = lambda cls: cls
    module.registered = []
    module.register_capability = module.registered.append
    module.host = types.SimpleNamespace(
        ui=types.SimpleNamespace(NotificationLevel=NotificationLevel, notifications=[], dialogs=[], answer="ok"),
        preset=types.SimpleNamespace(),
    )
    module.host.ui.push_notification = lambda level, text: module.host.ui.notifications.append((level, text))

    def message(text, title, buttons, icon):
        module.host.ui.dialogs.append(text)
        return module.host.ui.answer

    module.host.ui.message = message
    module.host.ui.WINDOW_MODAL = 1
    # What the picker showed, and how the user answers: an index per row (None cancels), given at once,
    # "later" while the plugin waits, or never because the plugin was "unloaded".
    module.host.ui.windows = []
    module.host.ui.picks = lambda rows: [0] * len(rows)
    module.host.ui.timing = "now"

    def create_window(html, title, width, height, on_close=None, style=0, on_submit=None):
        assert style == module.host.ui.WINDOW_MODAL
        data = json.loads(html.split('<script id="data" type="application/json">')[1].split("</script>")[0])
        module.host.ui.windows.append(SimpleNamespace(html=html, data=data, title=title, size=(width, height)))
        ui = module.host.ui

        def answer():
            picks = ui.picks(data["rows"])
            if picks is None:
                on_close()
            else:
                on_submit(picks if not isinstance(picks, list) else {"picks": picks})

        if ui.timing == "now":
            answer()
        state = {"asked": False}

        def is_open():
            if ui.timing == "later" and not state["asked"]:
                state["asked"] = True
                answer()
                return True
            return ui.timing != "unloaded"

        return SimpleNamespace(is_open=is_open)

    module.host.ui.create_window = create_window
    return module


@pytest.fixture
def orca(monkeypatch):
    module = fake_orca()
    monkeypatch.setitem(sys.modules, "orca", module)
    return module


@pytest.fixture
def plugin(orca):
    sys.modules.pop(MODULE, None)
    yield importlib.import_module(MODULE)
    sys.modules.pop(MODULE, None)


@pytest.fixture
def printer():
    server = FakePrinter()
    yield server
    server.server.shutdown()


@pytest.fixture
def app(orca, plugin, printer):
    """A running OrcaSlicer with the plugin loaded and a 0.4 mm nozzle.

    The printer preset has a PrusaLink connection with an API key. Leaving `slots` empty gives the project
    one filament slot, which holds the selected preset.
    """
    filaments = FakeFilaments()
    slots = []
    state = SimpleNamespace(nozzle="0.4", config={
        "host_type": "prusalink", "print_host": printer.address, "printhost_authorization_type": "key",
        "printhost_apikey": printer.api_key, "printhost_user": "", "printhost_password": "",
    })
    bundle = SimpleNamespace(
        filaments=filaments,
        full_config_value=lambda key: state.nozzle if key == "nozzle_diameter" else state.config.get(key),
        current_filament_preset_names=lambda: list(slots) or [filaments.selected.name],
    )
    orca.host.preset_bundle = lambda: bundle
    orca.host.preset.filament = FakeFilamentEditor(filaments, slots)
    command = plugin.MatchPrinterFilaments()
    command.config = {}
    return SimpleNamespace(
        filaments=filaments, state=state, plugin=plugin, orca=orca, printer=printer, slots=slots,
        editor=orca.host.preset.filament, match=command.execute, config=command.config,
        notifications=orca.host.ui.notifications, dialogs=orca.host.ui.dialogs,
    )
