# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "PrusaLink Filament Match"
# description = "Selects, for each filament slot, the installed filament preset that matches what a Prusa printer reports loaded in that tool."
# author = "hyiger"
# version = "0.1.0"
# type = "script"
# ///
"""PrusaLink Filament Match -- selects the filament presets that match what a Prusa printer holds.

"Match printer filaments" reads the vendor, material and color of each tool from a Prusa printer over
PrusaLink, and selects, for that tool's filament slot, the installed filament preset that fits it best:
any preset the slot's dropdown offers, from a vendor, a bundle or the user. Where several fit as well,
the user picks among them in an HTML dialog.

The printer reports these only with Buddy firmware that has Filament Slots, which serves them at
GET /api/v1/tools.
"""
import dataclasses
import http.client
import json
import math
import re
import threading
import urllib.error
import urllib.request

import orca

MATCH_NAME = "Match printer filaments"
RUN_LOCK = threading.Lock()
# OrcaSlicer asks in the background and denies the attempt that made it ask.
PERMISSION_TEXT = "OrcaSlicer needs your permission to reach this address. Allow it, then run this again."

# Buddy firmware with Filament Slots serves this; stock firmware answers 404.
TOOLS_PATH = "/api/v1/tools"
PRINTER_TIMEOUT_SECONDS = 10
# Below this CIE76 difference, two colors are one to the printer's color names, as white and off-white are.
COLOR_TOLERANCE = 25
# How a preset's color fits a tool's, best first. A name holding a color the user named on the printer,
# such as Galaxy Black, beats a close value.
NAMED_COLOR, SAME_COLOR, NO_COLOR, OTHER_COLOR = range(4)
# The printer's preset colors. A preset named for one of them is that color, whatever its color value.
PRINTER_COLORS = frozenset({"black", "blue", "brown", "cyan", "gold", "gray", "green", "magenta", "natural",
                            "orange", "pink", "purple", "red", "silver", "white", "yellow"})
# Filament DB gives a filament with no color this one, unless its name says it is gray.
BLANK_COLOR = (0x80, 0x80, 0x80)
FLEXIBLE_MATERIALS = frozenset({"flex", "peba", "tpe", "tpu"})
# The firmware keeps 7 characters of a material's name, so PPS-CF10 arrives as PPS-CF1.
MATERIAL_NAME_LENGTH = 7
# Prusa's slicers call Prusament's vendor Prusa Polymers.
VENDOR_ALIASES = {"prusapolymers": "prusament"}
# The firmware's user vendors and colors, which go by these until the user names them.
UNNAMED_ENTRY = re.compile(r"(vendor|color)[1-8]", re.IGNORECASE)
# Carbon or glass fiber, as in PA-CF, GF15, Carbon Fiber and PETG Carbon, but not Polycarbonate, nor the
# color Carbon Black.
ABRASIVE_WORDS = re.compile(r"""
    (?<![a-z]) (?: [cg]f\d* (?![a-z])
                 | carbon (?: [\W_]?fib | (?![a-z]) (?![\W_]*(?:black|gr[ae]y)(?![a-z])) )
                 | glass [\W_]?fib )
""", re.IGNORECASE | re.VERBOSE)
HEX_COLOR = re.compile(r"#?([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?")
NUMBER_LIST = re.compile(r"[+-]?\d*\.?\d+%?(,[+-]?\d*\.?\d+%?)*")
# The printer a preset is for, as in "Prusament PLA @CORE One HF 0.4" and "Prusament PLA @MK4S - Copy". Its
# words say nothing about the filament.
PRINTER_PART = re.compile(r"@[^-]*")
# OrcaSlicer's mark on a preset with unsaved edits.
MODIFIED_MARK = "* "

# The dialog where the user picks among presets that fit a tool as well, sized in DIPs. A row's detail
# wraps at about PICKER_DETAIL_LENGTH characters, and the rows scroll when they still do not fit.
PICKER_WIDTH = 680
PICKER_BASE_HEIGHT = 160
PICKER_ROW_HEIGHT = 62
PICKER_NOTE_HEIGHT = 18
PICKER_DETAIL_LENGTH = 55
PICKER_MAX_HEIGHT = 700
DIALOG_POLL_SECONDS = 0.25
PICKER_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><style>
/* OrcaSlicer injects its theme as --orca-* variables, and default styles for plain elements that may come
   after these rules, so the rules below name an id or class to win. */
:root { --fg: var(--orca-fg, #262e30); --bg: var(--orca-bg, #ffffff); --muted: var(--orca-muted, #6b6b6b);
        --line: var(--orca-border, #dbdbdb); --accent: var(--orca-accent, #009688); --on-accent: var(--orca-accent-fg, #ffffff); }
html:root { height: 100%; }
/* The buttons stay in view, and the rows above them scroll. */
html body { margin: 0; padding: 0; height: 100%; display: flex; flex-direction: column; color: var(--fg);
            background: var(--bg); font-size: 13px;
            font-family: var(--orca-font, -apple-system, "Segoe UI", system-ui, sans-serif); }
#content { flex: 1; min-height: 0; overflow-y: auto; padding: 18px 20px 0; }
#intro { margin: 0 0 12px; }
#rows { width: 100%; border-collapse: collapse; }
#rows td { padding: 9px 6px; border: 0; border-top: 1px solid var(--line); vertical-align: top; text-align: left; }
#rows td.slot { white-space: nowrap; font-weight: 600; }
#rows td.tool { width: 34%; }
.swatch { display: inline-block; flex: none; width: 12px; height: 12px; margin-right: 7px; vertical-align: -1px;
          border: 1px solid var(--line); border-radius: 3px; }
.choice { display: flex; align-items: center; }
.choice select { flex: 1; min-width: 0; font: inherit; }
.muted { color: var(--muted); font-size: 12px; margin-top: 4px; white-space: pre-line; }
#buttons { flex: none; display: flex; justify-content: flex-end; gap: 8px; padding: 14px 20px 18px; }
#buttons button { font: inherit; padding: 5px 18px; border-radius: 6px; border: 1px solid var(--line);
                  background: transparent; color: var(--fg); cursor: pointer; }
#buttons button.primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
</style></head><body>
<div id="content">
<p id="intro">The printer reports only vendor, material and color, so more than one installed preset fits some
tools. Pick what each slot holds.</p>
<table id="rows"></table>
<div id="notes" class="muted"></div>
</div>
<div id="buttons"><button id="cancel">Cancel</button><button id="ok" class="primary">Select</button></div>
<script id="data" type="application/json">__DATA__</script>
<script>
const data = JSON.parse(document.getElementById("data").textContent);
function swatch(color) {
  const mark = document.createElement("span");
  mark.className = "swatch";
  mark.style.background = color || "transparent";
  return mark;
}
const selects = data.rows.map(row => {
  const line = document.getElementById("rows").insertRow();
  const slot = line.insertCell();
  slot.className = "slot";
  slot.textContent = "Slot " + row.slot;
  const tool = line.insertCell();
  tool.className = "tool";
  tool.append(swatch(row.color), row.tool);
  const cell = line.insertCell();
  const choice = document.createElement("div");
  choice.className = "choice";
  const mark = swatch(row.options[0].color);
  const select = document.createElement("select");
  row.options.forEach((option, index) => select.add(new Option(option.label, index)));
  select.onchange = () => { mark.style.background = row.options[select.value].color || "transparent"; };
  if (row.options.length > 1) {
    choice.append(mark, select);
  } else {
    choice.append(mark, row.options[0].label);
  }
  const detail = document.createElement("div");
  detail.className = "muted";
  detail.textContent = row.detail;
  cell.append(choice, detail);
  return select;
});
document.getElementById("notes").textContent = data.notes;
const submit = () => window.orca.submit({picks: selects.map(select => Number(select.value))});
document.getElementById("ok").onclick = submit;
document.getElementById("cancel").onclick = () => window.orca.close();
document.addEventListener("keydown", event => {
  if (event.key === "Escape") window.orca.close();
  // A focused list or button handles its own Enter, so Enter on Cancel cancels.
  if (event.key === "Enter" && !["SELECT", "BUTTON"].includes(event.target.tagName)) submit();
});
</script></body></html>
"""


class MatchError(Exception):
    """A failure to report to the user as it stands."""


def decode_value(value):
    """Turn a serialized config value into a list of its items."""
    if NUMBER_LIST.fullmatch(value):
        return value.split(",")
    if not value.startswith('"'):
        return [value]
    # Strings are double-quoted and C-escaped, and separated by semicolons.
    items, current, position = [], [], 1
    while position < len(value):
        character = value[position]
        if character == "\\":
            position += 1
            current.append({"n": "\n", "r": "\r", "t": "\t"}.get(value[position:position + 1], value[position:position + 1]))
        elif character == '"':
            items.append("".join(current))
            current = []
            position = value.find('"', position + 1)
            if position < 0:
                break
        else:
            current.append(character)
        position += 1
    return items


def unescaped(text):
    """Undo the C-style escaping that OrcaSlicer applies to the value of a text setting."""
    return re.sub(r"\\(.)", lambda match: {"n": "\n", "r": "\r"}.get(match[1], match[1]), text)


def confirmed(question, title):
    """Ask *question* with OK and Cancel. OrcaSlicer shows it in a label, where a lone & marks a shortcut key
    and disappears, so names keep theirs."""
    return orca.host.ui.message(question.replace("&", "&&"), title, "ok_cancel", "question") == "ok"


def display_name(preset):
    """Return the name OrcaSlicer shows, without the mark it puts on a preset with unsaved edits."""
    return preset.label().removeprefix(MODIFIED_MARK)


class Printer:
    """The printer's PrusaLink connection. The plugin's own settings come first, so that a printer that prints
    through Prusa Connect can still be read; otherwise the connection OrcaSlicer uploads through."""

    def __init__(self, bundle, config):
        def setting(key):
            return unescaped(bundle.full_config_value(key) or "")

        def own(key):
            return str(config.get(key) or "")

        if host := own("address").strip():
            api_key, user, password = own("api_key").strip(), own("user").strip(), own("password")
            digest = bool(user) and not api_key
            self.settings = f"the Config tab of {MATCH_NAME}"
        else:
            host = setting("print_host").strip()
            if setting("host_type") != "prusalink" or not host:
                raise MatchError(f"Set address, and api_key or user and password, in the Config tab of {MATCH_NAME}. "
                                 "Or click Connection next to the printer, choose PrusaLink, and enter its address "
                                 "and API key or password.")
            api_key, user, password = setting("printhost_apikey"), setting("printhost_user"), setting("printhost_password")
            digest = setting("printhost_authorization_type") == "user"
            self.settings = "its connection settings"
        self.url = (host if host.startswith(("http://", "https://")) else "http://" + host).rstrip("/")
        self.headers, self.handlers = {}, []
        if digest:
            passwords = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            passwords.add_password(None, self.url, user, password)
            self.handlers.append(urllib.request.HTTPDigestAuthHandler(passwords))
        else:
            self.headers["X-Api-Key"] = api_key

    def tools(self):
        """Return {number: Tool} for the tools the printer reports, and the keys of those it could not read."""
        request = urllib.request.Request(self.url + TOOLS_PATH, headers=self.headers)
        try:
            with urllib.request.build_opener(*self.handlers).open(request, timeout=PRINTER_TIMEOUT_SECONDS) as response:
                return parse_tools(json.load(response))
        except PermissionError as error:
            raise MatchError(PERMISSION_TEXT) from error
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise MatchError(f"The printer refused the API key or password. Check them in {self.settings}, "
                                 "and that login is enabled on the printer.") from error
            if error.code == 404:
                raise MatchError(f"The printer at {self.url} does not report what its tools hold. "
                                 "It needs Buddy firmware with Filament Slots.") from error
            raise MatchError(f"The printer at {self.url} returned an error: HTTP {error.code}.") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise MatchError(f"Could not reach the printer at {self.url}. Is it on?") from error
        # urllib wraps only the errors of sending the request: these come from a bad port or from the reply.
        except http.client.InvalidURL as error:
            raise MatchError(f"The printer address {self.url} is not valid. Check it in {self.settings}.") from error
        except (http.client.HTTPException, ConnectionError) as error:
            raise MatchError(f"The printer at {self.url} closed the connection before it answered. "
                             "Run this again.") from error
        except ValueError as error:
            raise MatchError(f"The printer at {self.url} sent a reply that is not JSON.") from error


@dataclasses.dataclass
class Tool:
    """What the printer reports about one tool. An empty tool has no material."""
    number: int
    material: str
    vendor: str
    color: str
    color_hex: tuple | None
    nozzle: float | None
    hardened: bool

    def describe(self):
        return " ".join(part for part in (self.vendor, self.material) if part)


def parse_color(text):
    """Return the (r, g, b) of a #RRGGBB or #RRGGBBAA color, or None."""
    match = HEX_COLOR.fullmatch(text.strip())
    return tuple(bytes.fromhex(match[1])) if match else None


def read_tool(key, entry):
    """Return the tool that the printer reports under *key*, or None if the entry cannot be read."""
    if not (re.fullmatch(r"[1-9]\d*", key) and isinstance(entry, dict) and isinstance(entry.get("loaded"), bool)):
        return None
    texts = [entry.get(field, "") for field in ("material", "vendor", "color", "color_hex")]
    if not all(isinstance(text, str) for text in texts):
        return None
    material, vendor, color, color_hex = (text.strip() for text in texts)
    nozzle = entry.get("nozzle_diameter")
    return Tool(
        number=int(key),
        material=material if entry["loaded"] else "",
        vendor="" if UNNAMED_ENTRY.fullmatch(vendor) else vendor,
        color="" if UNNAMED_ENTRY.fullmatch(color) else color,
        color_hex=parse_color(color_hex),
        nozzle=nozzle if type(nozzle) in (int, float) else None,
        hardened=entry.get("hardened") is True,
    )


def parse_tools(body):
    """Read the reply to GET /api/v1/tools. Tool N is filament slot N, so a gap in the numbers stays."""
    tools = body.get("tools") if isinstance(body, dict) else None
    if not isinstance(tools, dict):
        raise MatchError("The printer's reply does not list its tools.")
    read = {key: read_tool(key, entry) for key, entry in tools.items()}
    return ({tool.number: tool for tool in read.values() if tool},
            sorted(key for key, tool in read.items() if not tool))


def lab(rgb):
    """Convert an sRGB color to CIELAB, under D65."""
    def linear(channel):
        channel /= 255
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    def f(t):
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    r, g, b = (linear(channel) for channel in rgb)
    x = f((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047)
    y = f(0.2126 * r + 0.7152 * g + 0.0722 * b)
    z = f((0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
    return 116 * y - 16, 500 * (x - y), 200 * (y - z)


def color_difference(first, second):
    """The CIE76 difference between two colors."""
    return math.dist(lab(first), lab(second))


def words(text):
    """The words of *text*, folded for comparison, with grey spelled gray."""
    return {"gray" if word == "grey" else word for word in re.findall(r"[0-9a-z]+", text.casefold())}


def vendor_key(vendor):
    """Fold a vendor's name, so that 3D-Fuel is 3D Fuel and eSun is Esun."""
    folded = re.sub(r"[^0-9a-z]", "", vendor.casefold())
    return VENDOR_ALIASES.get(folded, folded)


def material_key(material):
    folded = material.strip().casefold()
    return "flex" if folded in FLEXIBLE_MATERIALS else folded


def same_material(reported, material):
    """Tell whether the printer's material is *material*. PETG and PET stay apart."""
    reported, material = material_key(reported), material_key(material)
    return reported == material or (len(reported) == MATERIAL_NAME_LENGTH and material.startswith(reported))


@dataclasses.dataclass
class Candidate:
    """A filament preset, as far as matching it to a tool goes. *named* holds the words its color and kind are
    read from, and *system* tells a vendor's preset from one the user made or installed."""
    name: str
    label: str
    vendor: str
    material: str
    color: tuple | None
    abrasive: bool
    named: frozenset
    system: bool = False


IDENTITY_KEYS = ("default_filament_colour", "filament_type", "filament_vendor", "required_nozzle_HRC")


def candidate(name, label, values, parent="", system=False):
    """Describe the preset *name* from *values*, which maps a setting to its list of values.

    A user preset is also read by the name of the preset it inherits from, *parent*, as the user may have
    named it for something else, such as My Tuned PLA.
    """
    def first(key):
        return (values.get(key) or [""])[0].strip()

    material = first("filament_type")
    label_words, parent_words = (PRINTER_PART.sub(" ", text) for text in (label, parent))
    named = frozenset(words(f"{label_words} {parent_words}"))
    color = parse_color(first("default_filament_colour"))
    if color == BLANK_COLOR and "gray" not in named:
        color = None
    # The INDX profiles treat a filament that needs a hardened nozzle as abrasive.
    abrasive = first("required_nozzle_HRC") not in ("", "0") or any(
        ABRASIVE_WORDS.search(text) for text in (label_words, parent_words, material))
    return Candidate(name, label, first("filament_vendor"), material, color, abrasive, named, system)


def preset_candidate(preset, label=None):
    values = {key: decode_value(preset.config_value(key) or "") for key in IDENTITY_KEYS}
    parent = unescaped(preset.config_value("inherits") or "")
    return candidate(preset.name, label or display_name(preset), values, parent, preset.is_system)


def installed_presets(filaments):
    """The filament presets that a slot's dropdown offers: those shown and compatible with the printer.

    Where two show the same name, as a vendor's preset for two nozzles may, each is labelled by its own name.
    """
    offered = [preset for preset in (filaments.preset(index) for index in range(filaments.size()))
               if preset.is_visible and preset.is_compatible and not preset.is_default]
    labels = [display_name(preset) for preset in offered]
    return [preset_candidate(preset, label if labels.count(label) == 1 else preset.name)
            for preset, label in zip(offered, labels)]


def color_fit(tool, preset):
    """Return NAMED_COLOR, SAME_COLOR, NO_COLOR or OTHER_COLOR for the color of *preset* against the tool's.

    A name says more than a color value, which a preset may leave at a default. A tool without a color counts
    against no preset.
    """
    if tool.color_hex is None:
        return SAME_COLOR
    named, label = words(tool.color), preset.named
    if named and named <= label:
        # One of the printer's own colors, such as White, also covers close shades named otherwise, such as Ivory.
        return NAMED_COLOR if named - PRINTER_COLORS else SAME_COLOR
    # A tool named for one of the printer's colors is not another of them. A color the user named otherwise,
    # such as Navy, may be any of them, so the color value decides.
    if named & PRINTER_COLORS and label & (PRINTER_COLORS - named):
        return OTHER_COLOR
    if preset.color is None:
        return NO_COLOR
    return SAME_COLOR if color_difference(tool.color_hex, preset.color) <= COLOR_TOLERANCE else OTHER_COLOR


def fit(tool, preset):
    """What tells presets apart for the printer, which knows a tool's vendor, material and color only."""
    return preset.abrasive and not tool.hardened, color_fit(tool, preset)


def preference(tool, preset):
    """Order presets that fit equally: Generic first for a tool without a vendor, then the user's own presets
    before a vendor's, then the name that says the least beyond vendor, material and color (PLA White before
    PLA Silk White), then the closest color."""
    extra = preset.named - words(preset.vendor) - words(preset.material) - words(tool.color)
    distance = (color_difference(tool.color_hex, preset.color)
                if tool.color_hex is not None and preset.color is not None else math.inf)
    return (*fit(tool, preset), not tool.vendor and vendor_key(preset.vendor) != "generic", preset.system,
            len(extra), distance, preset.label.casefold())


def matches(tool, preset):
    """Vendor and material must match. A tool without a vendor goes by its material alone."""
    return same_material(tool.material, preset.material) and (
        not tool.vendor or vendor_key(tool.vendor) == vendor_key(preset.vendor))


def options(tool, presets, current):
    """Return (presets, kept, abrasive): the presets that fit the tool as well as the best one, the default
    first, and the abrasive preset of the tool's color that a nozzle which is not hardened passed over.

    The printer reports only vendor, material and color, so it cannot tell plain from matte, silk or carbon
    fiber filament, nor Snow White from Translucent White: the user picks among presets that fit as well.
    The slot's own preset is the default when it fits as well, since the user chose it.
    """
    ranked = sorted((preset for preset in presets if matches(tool, preset)), key=lambda preset: preference(tool, preset))
    kept = current is not None and matches(tool, current) and (not ranked or fit(tool, current) <= fit(tool, ranked[0]))
    if kept:
        ranked = [current, *(preset for preset in ranked if preset.name != current.name)]
    best = [preset for preset in ranked if fit(tool, preset) == fit(tool, ranked[0])]
    color = color_fit(tool, best[0]) if best else OTHER_COLOR
    abrasive = next((preset for preset in ranked if fit(tool, preset)[0]
                     and color_fit(tool, preset) < color and color_fit(tool, preset) <= SAME_COLOR), None)
    return best, kept, abrasive


@dataclasses.dataclass
class Step:
    """What one loaded tool's slot can get, the default first. A slot with no option is left alone."""
    tool: Tool
    was: str
    options: list
    kept: bool
    abrasive: Candidate | None = None

    @property
    def preset(self):
        return self.options[0] if self.options else None


def numbered(noun, numbers):
    joined = ", ".join(map(str, numbers[:-1])) + " and " + str(numbers[-1]) if len(numbers) > 1 else str(numbers[0])
    return f"{noun}{'s' if len(numbers) > 1 else ''} {joined}"


def step_notes(step, nozzles):
    """What to know about the slot's preset. Every option fits the tool as well, so they share these."""
    tool = step.tool
    abrasive, color = fit(tool, step.preset)
    notes = []
    tool_color = tool.color or css_color(tool.color_hex)
    if step.abrasive is not None:
        notes.append(f"{step.abrasive.label} is {tool_color}, but abrasive, and the nozzle is not hardened")
    elif color > SAME_COLOR:
        notes.append(f"no installed {tool.describe()} preset is {tool_color}")
    if abrasive:
        notes.append("it is abrasive, and the nozzle is not hardened")
    if not tool.vendor and not step.kept:
        notes.append("the printer names no vendor")
    profile_nozzle = nozzles[min(tool.number, len(nozzles)) - 1] if nozzles else None
    if tool.nozzle is not None and profile_nozzle is not None and abs(tool.nozzle - profile_nozzle) > 0.001:
        notes.append(f"the tool has a {tool.nozzle:g} mm nozzle, and the printer profile {profile_nozzle:g} mm")
    return notes


def step_line(step, nozzles):
    tool = step.tool
    if step.preset is None:
        return f"Slot {tool.number}: left alone, as no installed preset is {tool.describe()}"
    notes = step_notes(step, nozzles)
    text = (f"Slot {tool.number}: keeps {step.preset.label}" if step.kept
            else f"Slot {tool.number}: {step.preset.label}, in place of {step.was}")
    return text + (f" ({'; '.join(notes)})" if notes else "")


def css_color(rgb):
    return "#%02X%02X%02X" % rgb if rgb else ""


def show_dialog(page, height):
    """Show *page* as a modal dialog and wait for it. Return what it submitted, or None if it was closed."""
    answers, done = [], threading.Event()

    def submitted(data):
        answers.append(data)
        done.set()

    window = orca.host.ui.create_window(page, MATCH_NAME, PICKER_WIDTH, height, on_close=done.set,
                                        style=orca.host.ui.WINDOW_MODAL, on_submit=submitted)
    # OrcaSlicer calls back on its own thread. Unloading the plugin closes the dialog without a call,
    # so a closed window ends the wait too.
    while not done.wait(DIALOG_POLL_SECONDS):
        if not window.is_open():
            break
    return answers[0] if answers else None


def pick(steps, nozzles, notes):
    """Ask which preset each slot gets, where several fit as well. Return [(step, preset)], or None when the
    user cancels. *notes* says what happens to the tools that get no preset."""
    shown = [step for step in steps if step.options]
    rows = [{
        "slot": step.tool.number,
        "tool": " ".join(part for part in (step.tool.vendor, step.tool.material, step.tool.color) if part),
        "color": css_color(step.tool.color_hex),
        "detail": "; ".join([f"Now {step.was}", *step_notes(step, nozzles)]),
        "options": [{"label": preset.label, "color": css_color(preset.color)} for preset in step.options],
    } for step in shown]
    # "<" is escaped so that no label can end the script element.
    data = json.dumps({"rows": rows, "notes": "\n".join(notes)}).replace("<", "\\u003c")
    lines = len(notes) + sum(len(row["detail"]) // PICKER_DETAIL_LENGTH for row in rows)
    height = min(PICKER_MAX_HEIGHT, PICKER_BASE_HEIGHT + PICKER_ROW_HEIGHT * len(rows) + PICKER_NOTE_HEIGHT * lines)
    answer = show_dialog(PICKER_PAGE.replace("__DATA__", data), height)
    picks = answer.get("picks") if isinstance(answer, dict) else None
    if not (isinstance(picks, list) and len(picks) == len(shown) and all(
            type(index) is int and 0 <= index < len(step.options) for index, step in zip(picks, shown))):
        return None
    return [(step, step.options[index]) for index, step in zip(picks, shown)]


def match(config):
    """Select, for each loaded tool of the printer, the installed preset that fits it best, or the one the user
    picks where several fit as well."""
    bundle = orca.host.preset_bundle()
    tools, unreadable = Printer(bundle, config).tools()
    if not tools and not unreadable:
        raise MatchError("The printer reports no tools, so none of its docks is calibrated yet.")

    presets = installed_presets(bundle.filaments)
    slot_names = list(bundle.current_filament_preset_names())
    # A slot may hold a preset the dropdown no longer offers, such as a hidden one, and it can keep it.
    current = [preset_candidate(preset) if (preset := bundle.filaments.find_preset(name)) is not None else None
               for name in slot_names]
    nozzles = [float(item) for item in (bundle.full_config_value("nozzle_diameter") or "").split(",") if item]
    loaded = [tool for _, tool in sorted(tools.items()) if tool.material]
    steps = [Step(tool, current[tool.number - 1].label if current[tool.number - 1] else slot_names[tool.number - 1],
                  *options(tool, presets, current[tool.number - 1]))
             for tool in loaded if tool.number <= len(slot_names)]

    lines = [step_line(step, nozzles) for step in steps]
    others = []
    if empty := [tool.number for _, tool in sorted(tools.items()) if not tool.material]:
        others.append(f"Empty on the printer, so left alone: {numbered('tool', empty)}")
    if beyond := [tool.number for tool in loaded if tool.number > len(slot_names)]:
        slots = f"{len(slot_names)} filament slot{'s' if len(slot_names) != 1 else ''}"
        others.append(f"Not in this project, which has {slots}: {numbered('tool', beyond)}")
    if unreadable:
        others.append(f"Could not read what the printer sent for {numbered('tool', unreadable)}")

    # Where several presets fit a tool as well, the user picks; that dialog also confirms the rest.
    asked = any(len(step.options) > 1 for step in steps)
    if asked:
        picked = pick(steps, nozzles, [line for step, line in zip(steps, lines) if not step.options] + others)
        if picked is None:
            raise MatchError("Nothing was changed.")
    else:
        picked = [(step, step.preset) for step in steps if step.options]
    changes = [(step, preset) for step, preset in picked if preset.name != slot_names[step.tool.number - 1]]
    lines += others
    if not changes:
        return "Nothing to change.\n\n" + "\n".join(lines)
    question = "Select these filament presets to match the printer?\n\n" + "\n".join(lines)
    if not asked and not confirmed(question, MATCH_NAME):
        raise MatchError("Nothing was changed.")

    selected, refused = [], []
    for step, preset in changes:
        try:
            orca.host.preset.filament.begin_update(slot_number=step.tool.number, preset_name=preset.name).commit()
        except ValueError as error:
            # Such as a mixed-color slot, which has no preset of its own.
            refused.append(f"OrcaSlicer would not select {preset.label} for slot {step.tool.number}: {error}")
        except RuntimeError as error:
            # The user cancelled the prompt about unsaved edits, so the rest is left as it is.
            done = f" Slots changed before it: {', '.join(map(str, selected))}." if selected else ""
            raise MatchError(f"Stopped at slot {step.tool.number}: {error}.{done}") from error
        else:
            selected.append(step.tool.number)
    if not selected:
        raise MatchError("\n".join([*refused, "Nothing was changed."]))
    return "\n".join([f"Selected filament presets for {numbered('slot', selected)}.", *refused])


def run_match(config):
    """Run the match and show its outcome, since a script's return value is easy to miss."""
    levels = orca.host.ui.NotificationLevel
    locked = RUN_LOCK.acquire(blocking=False)
    try:
        if not locked:
            raise MatchError(f"{MATCH_NAME} is still running.")
        message = match(config)
    except MatchError as error:
        message = str(error)
    except Exception as error:
        # OrcaSlicer disables a plugin whose script raises, which would remove the command from the
        # Plugins window over something as passing as a preset it refused to select.
        message = f"{MATCH_NAME} failed: {error}"
    else:
        orca.host.ui.push_notification(levels.RegularNotificationLevel, message)
        return orca.ExecutionResult.success(message)
    finally:
        if locked:
            RUN_LOCK.release()
    orca.host.ui.push_notification(levels.WarningNotificationLevel, message)
    return orca.ExecutionResult.skipped(message)


class MatchPrinterFilaments(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return MATCH_NAME

    def get_default_config(self):
        return {"address": "", "api_key": "", "user": "", "password": ""}

    def execute(self):
        return run_match(json.loads(self.get_config()))


@orca.plugin
class PrusaLinkFilamentMatch(orca.base):
    def register_capabilities(self):
        orca.register_capability(MatchPrinterFilaments)
