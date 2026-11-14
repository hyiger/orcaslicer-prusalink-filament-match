"""Match printer filaments: the printer's tools, read over PrusaLink, against the installed filament presets."""
import urllib.request

import pytest

from conftest import NotificationLevel, PluginResult, preset

# The project's preset to begin with: a vendor's preset, shown by its alias.
SYSTEM_NAME = "Generic PLA @System"
SYSTEM = "Generic PLA"


def stock(app, *presets):
    """Install *presets* as the user's own, after the vendor's."""
    app.filaments.add(*presets)


def filament(name, vendor, material, color=None, **extra):
    return preset(name, vendor, material, color, **extra)


def load(app, *tools):
    """Put *tools* in the printer as tools 1, 2 and so on. None leaves a dock uncalibrated."""
    app.printer.tools = {str(number): tool for number, tool in enumerate(tools, 1) if tool is not None}


def slots(app, *names):
    app.slots[:] = names


def selected(app):
    return list(app.editor.selections)


def picker_rows(app):
    """The rows of the one picker this run showed."""
    [window] = app.orca.host.ui.windows
    return window.data["rows"]


def labels(row):
    return [option["label"] for option in row["options"]]


class TestReadingThePrinter:
    def red(self, app):
        stock(app, filament("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        load(app, app.printer.tool("PLA", "Spike", "Red", "#FF0000"))

    def test_sends_the_api_key(self, app):
        self.red(app)
        assert app.match().status is PluginResult.Success
        path, headers = app.printer.requests[-1]
        assert path == "/api/v1/tools"
        assert headers["X-Api-Key"] == app.printer.api_key

    def test_signs_in_with_digest(self, app):
        """The firmware's Digest is RFC 2069, with no qop, which urllib still answers."""
        self.red(app)
        app.printer.password = 'pa"ss\\word'
        app.state.config.update(printhost_authorization_type="user", printhost_apikey="",
                                printhost_user="maker", printhost_password='pa\\"ss\\\\word')
        assert app.match().status is PluginResult.Success
        assert "Digest" in app.printer.requests[-1][1]["Authorization"]
        assert selected(app) == [(1, "Spike PLA Red")]

    @pytest.mark.parametrize("change", [
        {"printhost_apikey": "wrong"},
        {"printhost_authorization_type": "user", "printhost_user": "maker", "printhost_password": "wrong"},
    ])
    def test_a_refused_login_says_where_to_fix_it(self, app, change):
        app.state.config.update(change)
        result = app.match()
        assert result.status is PluginResult.Skipped
        assert result.message == ("The printer refused the API key or password. Check them in its connection "
                                  "settings, and that login is enabled on the printer.")
        assert app.notifications[-1] == (NotificationLevel.WarningNotificationLevel, result.message)

    @pytest.mark.parametrize("change", [{"host_type": "octoprint"}, {"print_host": " "}, {"host_type": None}])
    def test_needs_a_prusalink_connection(self, app, change):
        app.state.config.update(change)
        assert app.match().message == (
            "Set address, and api_key or user and password, in the Config tab of Match printer filaments. "
            "Or click Connection next to the printer, choose PrusaLink, and enter its address and API key or password.")
        assert not app.printer.requests

    def own_settings(self, app, **settings):
        """The plugin's own settings, for a printer that prints through Prusa Connect."""
        app.state.config.update(host_type="prusaconnect", print_host="https://connect.prusa3d.com", printhost_apikey="")
        app.config.update(address=f" {app.printer.address} ", **settings)
        self.red(app)

    def test_reads_the_printer_from_its_own_settings(self, app):
        self.own_settings(app, api_key=app.printer.api_key)
        assert app.match().status is PluginResult.Success
        assert app.printer.requests[-1][1]["X-Api-Key"] == app.printer.api_key
        assert selected(app) == [(1, "Spike PLA Red")]

    def test_its_own_settings_sign_in_with_digest(self, app):
        self.own_settings(app, user=" maker ", password=app.printer.password)
        assert app.match().status is PluginResult.Success
        assert "Digest" in app.printer.requests[-1][1]["Authorization"]

    def test_its_own_settings_come_before_the_printers_connection(self, app):
        self.own_settings(app, api_key=app.printer.api_key)
        app.state.config.update(host_type="prusalink", print_host="127.0.0.1:9", printhost_apikey="wrong")
        assert app.match().status is PluginResult.Success

    def test_a_refused_login_from_its_own_settings_says_where_to_fix_it(self, app):
        self.own_settings(app, api_key="wrong")
        assert app.match().message == ("The printer refused the API key or password. Check them in the Config tab "
                                       "of Match printer filaments, and that login is enabled on the printer.")

    def test_takes_an_address_with_a_scheme_and_a_slash(self, app):
        app.state.config["print_host"] = f"http://{app.printer.address}/"
        self.red(app)
        assert app.match().status is PluginResult.Success

    def test_stock_firmware_has_no_tools_to_report(self, app):
        app.printer.status = 404
        assert app.match().message == (f"The printer at http://{app.printer.address} does not report what its "
                                       "tools hold. It needs Buddy firmware with Filament Slots.")

    def test_reports_another_error_by_its_status(self, app):
        app.printer.status = 503
        assert app.match().message == f"The printer at http://{app.printer.address} returned an error: HTTP 503."

    def test_an_unreachable_printer_is_reported(self, app):
        app.printer.server.shutdown()
        app.printer.server.server_close()
        assert app.match().message == f"Could not reach the printer at http://{app.printer.address}. Is it on?"

    def test_a_reply_that_is_not_json_is_reported(self, app):
        app.printer.body = b"<html>"
        assert app.match().message == f"The printer at http://{app.printer.address} sent a reply that is not JSON."

    def test_a_dropped_connection_is_reported(self, app):
        """urllib wraps only errors in sending the request, not in reading the reply."""
        app.printer.drop = True
        assert app.match().message == (f"The printer at http://{app.printer.address} closed the connection "
                                       "before it answered. Run this again.")

    def test_a_reply_cut_short_is_reported(self, app, monkeypatch):
        def cut(*args, **kwargs):
            raise app.plugin.http.client.IncompleteRead(b"{")
        monkeypatch.setattr(urllib.request.OpenerDirector, "open", cut)
        assert "closed the connection before it answered" in app.match().message

    def test_an_address_with_a_bad_port_is_reported(self, app):
        app.config.update(address="127.0.0.1:abc", api_key=app.printer.api_key)
        assert app.match().message == ("The printer address http://127.0.0.1:abc is not valid. "
                                       "Check it in the Config tab of Match printer filaments.")

    @pytest.mark.parametrize("body", [b"[]", b'{"tools": []}', b"{}"])
    def test_a_reply_without_tools_is_reported(self, app, body):
        app.printer.body = body
        assert app.match().message == "The printer's reply does not list its tools."

    def test_asks_for_permission(self, app, monkeypatch):
        def denied(*args, **kwargs):
            raise PermissionError("blocked")
        monkeypatch.setattr(urllib.request.OpenerDirector, "open", denied)
        assert app.match().message == app.plugin.PERMISSION_TEXT

    def test_no_calibrated_dock_means_no_tools(self, app):
        load(app)
        assert app.match().message == "The printer reports no tools, so none of its docks is calibrated yet."


class TestReadingATool:
    def test_reads_every_field(self, plugin):
        tools, unreadable = plugin.parse_tools({"tools": {"2": {
            "loaded": True, "material": " PETG ", "vendor": "Prusament", "color": "Galaxy Black",
            "color_hex": "#3D3E3DFF", "nozzle_diameter": 0.6, "hardened": True, "high_flow": False,
        }}})
        assert unreadable == []
        assert tools == {2: plugin.Tool(2, "PETG", "Prusament", "Galaxy Black", (0x3D, 0x3E, 0x3D), 0.6, True)}

    def test_an_empty_tool_has_no_material(self, plugin):
        tools, _ = plugin.parse_tools({"tools": {"1": {"loaded": False, "material": "PLA"}}})
        assert tools[1].material == ""

    @pytest.mark.parametrize("vendor, color", [("VENDOR3", "COLOR8"), ("vendor1", "color2")])
    def test_an_unnamed_user_entry_counts_as_no_name(self, plugin, vendor, color):
        tools, _ = plugin.parse_tools({"tools": {"1": {"loaded": True, "material": "PLA", "vendor": vendor,
                                                         "color": color, "color_hex": "#123456"}}})
        assert (tools[1].vendor, tools[1].color, tools[1].color_hex) == ("", "", (0x12, 0x34, 0x56))

    @pytest.mark.parametrize("entry, field, expected", [
        ({"nozzle_diameter": True}, "nozzle", None),
        ({"nozzle_diameter": "0.4"}, "nozzle", None),
        ({"nozzle_diameter": 1}, "nozzle", 1),
        ({"hardened": "true"}, "hardened", False),
        ({"color_hex": "red"}, "color_hex", None),
        ({}, "color_hex", None),
    ])
    def test_a_field_of_another_type_is_not_taken(self, plugin, entry, field, expected):
        tools, _ = plugin.parse_tools({"tools": {"1": {"loaded": True, "material": "PLA", **entry}}})
        assert getattr(tools[1], field) == expected

    def test_entries_it_cannot_read_are_listed(self, plugin):
        tools, unreadable = plugin.parse_tools({"tools": {
            "0": {"loaded": True}, "x": {"loaded": True}, "3": [], "4": {"loaded": "yes"},
            "5": {"loaded": True, "material": 5}, "6": {"loaded": True, "material": "PLA"},
        }})
        assert list(tools) == [6]
        assert unreadable == ["0", "3", "4", "5", "x"]


class TestInstalledPresets:
    """Every preset the slot's dropdown offers is a candidate, whoever made it."""

    def test_a_vendors_preset_is_matched_by_its_alias(self, app):
        """Its name ends in the printer it is for, which says nothing about the filament."""
        stock(app, filament("Prusament PLA @CORE One", "Prusa Polymers", "PLA", "#FF8000", alias="Prusament PLA",
                            is_system=True))
        load(app, app.printer.tool("PLA", "Prusament", "Orange", "#FF8000"))
        assert app.match().message == "Selected filament presets for slot 1."
        assert app.dialogs[-1].endswith(f"Slot 1: Prusament PLA, in place of {SYSTEM}")
        assert selected(app) == [(1, "Prusament PLA @CORE One")]

    def test_hidden_incompatible_and_default_presets_are_not_offered(self, app):
        stock(app, filament("Spike PLA Red Hidden", "Spike", "PLA", "#FF0000", is_visible=False),
              filament("Spike PLA Red @MK3S", "Spike", "PLA", "#FF0000", is_compatible=False),
              filament("Spike PLA", "Spike", "PLA", None))
        app.filaments.presets["Default Filament"].values.update(filament_vendor="Spike", default_filament_colour="#FF0000")
        app.filaments.presets["Default Filament"].is_visible = True
        load(app, app.printer.tool("PLA", "Spike", "Red", "#FF0000"))
        app.match()
        assert selected(app) == [(1, "Spike PLA")]

    def test_the_users_own_preset_comes_before_a_vendors(self, app):
        stock(app, filament("Spike PLA Red @CORE One", "Spike", "PLA", "#FF0000", alias="Spike PLA Red", is_system=True),
              filament("Spike PLA Red Tuned", "Spike", "PLA", "#FF0000"))
        load(app, app.printer.tool("PLA", "Spike", "Red", "#FF0000"))
        app.match()
        assert labels(picker_rows(app)[0]) == ["Spike PLA Red Tuned", "Spike PLA Red"]
        assert selected(app) == [(1, "Spike PLA Red Tuned")]

    def test_presets_shown_by_one_name_are_told_apart_by_their_own(self, app):
        """A vendor may have a preset for each nozzle, all shown by the same alias."""
        stock(app, *(filament(f"Spike PLA Red @CORE One{nozzle}", "Spike", "PLA", "#FF0000", alias="Spike PLA Red",
                              is_system=True) for nozzle in ("", " HF 0.4")))
        load(app, app.printer.tool("PLA", "Spike", "Red", "#FF0000"))
        app.match()
        assert labels(picker_rows(app)[0]) == ["Spike PLA Red @CORE One", "Spike PLA Red @CORE One HF 0.4"]

    def test_words_after_the_printer_still_count(self, app):
        stock(app, filament("Spike PLA @CORE One - Galaxy Black", "Spike", "PLA", "#000000"),
              filament("Spike PLA Jet", "Spike", "PLA", "#000000"))
        load(app, app.printer.tool("PLA", "Spike", "Galaxy Black", "#101010"))
        app.match()
        assert selected(app) == [(1, "Spike PLA @CORE One - Galaxy Black")]
        assert not app.orca.host.ui.windows

    def test_reads_a_vendor_value_with_spaces(self, app):
        stock(app, filament("3D Fuel Pro PCTG", "3D Fuel", "PCTG", "#FFFFFF"))
        load(app, app.printer.tool("PCTG", "3D-Fuel", "White", "#FFFFFF"))
        app.match()
        assert selected(app) == [(1, "3D Fuel Pro PCTG")]


class TestChoosing:
    def test_selects_the_vendor_material_and_color(self, app):
        stock(app, filament("Polymaker PLA Blue", "Polymaker", "PLA", "#0000FF"),
              filament("Overture PLA Red", "Overture", "PLA", "#FF0000"),
              filament("Polymaker PLA Red", "Polymaker", "PLA", "#FF0000"),
              filament("Polymaker PETG Red", "Polymaker", "PETG", "#FF0000"))
        load(app, app.printer.tool("PLA", "Polymaker", "Red", "#FF0000"))
        result = app.match()
        assert app.dialogs == ["Select these filament presets to match the printer?\n\n"
                               f"Slot 1: Polymaker PLA Red, in place of {SYSTEM}"]
        assert selected(app) == [(1, "Polymaker PLA Red")]
        assert result.status is PluginResult.Success
        assert result.message == "Selected filament presets for slot 1."
        assert app.slots == ["Polymaker PLA Red"]

    @pytest.mark.parametrize("printer_vendor, preset_vendor", [
        ("3D-Fuel", "3D Fuel"), ("eSun", "Esun"), ("Prusament", "Prusa Polymers"), ("Prusa Polymers", "Prusament"),
    ])
    def test_vendors_match_without_case_or_punctuation(self, app, printer_vendor, preset_vendor):
        stock(app, filament(f"{preset_vendor} PLA", preset_vendor, "PLA", "#FF0000"))
        load(app, app.printer.tool("PLA", printer_vendor, "Red", "#FF0000"))
        app.match()
        assert selected(app) == [(1, f"{preset_vendor} PLA")]

    @pytest.mark.parametrize("printer_material, preset_material", [
        ("FLEX", "TPU"), ("TPE", "PEBA"), ("pla", "PLA"), ("PPS-CF1", "PPS-CF10"),
    ])
    def test_materials_match_as_the_printer_names_them(self, app, printer_material, preset_material):
        stock(app, filament("Spike Filament", "Spike", preset_material, "#FF0000"))
        load(app, app.printer.tool(printer_material, "Spike", "Red", "#FF0000"))
        app.match()
        assert selected(app) == [(1, "Spike Filament")]

    @pytest.mark.parametrize("printer_material, preset_material", [("PETG", "PET"), ("PET", "PETG"), ("PA", "PA11")])
    def test_other_materials_do_not_match(self, app, printer_material, preset_material):
        stock(app, filament("Spike Filament", "Spike", preset_material, "#FF0000"))
        load(app, app.printer.tool(printer_material, "Spike", "Red", "#FF0000"))
        result = app.match()
        assert result.message == ("Nothing to change.\n\n"
                                  f"Slot 1: left alone, as no installed preset is Spike {printer_material}")
        assert not app.dialogs and not app.editor.selections

    def test_another_vendor_is_never_selected(self, app):
        stock(app, filament("Polymaker PLA Red", "Polymaker", "PLA", "#FF0000"))
        load(app, app.printer.tool("PLA", "Siraya Tech", "Red", "#FF0000"))
        assert app.match().message.endswith("no installed preset is Siraya Tech PLA")
        assert not app.editor.selections

    def test_a_tool_without_a_vendor_prefers_generic(self, app):
        stock(app, filament("Overture PLA Red", "Overture", "PLA", "#FF0000"),
              filament("Generic PLA Red", "Generic", "PLA", "#FF0000"),
              filament("Generic PLA Blue", "Generic", "PLA", "#0000FF"))
        slots(app, "Generic PLA Blue")
        load(app, app.printer.tool("PLA", "", "Red", "#FF0000"))
        app.match()
        assert selected(app) == [(1, "Generic PLA Red")]
        [row] = picker_rows(app)
        assert labels(row) == ["Generic PLA Red", "Overture PLA Red"]
        assert row["detail"] == "Now Generic PLA Blue; the printer names no vendor"

    def test_color_counts_before_generic(self, app):
        stock(app, filament("Overture PLA Red", "Overture", "PLA", "#FF0000"),
              filament("Generic PLA Blue", "Generic", "PLA", "#0000FF"))
        load(app, app.printer.tool("PLA", "", "Red", "#FF0000"))
        app.match()
        assert selected(app) == [(1, "Overture PLA Red")]


class TestColors:
    def test_a_name_with_the_tools_color_comes_first(self, app):
        stock(app, filament("Prusament PLA Galaxy Black", "Prusament", "PLA", "#3D3E3D"),
              filament("Prusament PLA Jet", "Prusament", "PLA", "#000000"))
        load(app, app.printer.tool("PLA", "Prusament", "Galaxy Black", "#101010"))
        app.match()
        assert selected(app) == [(1, "Prusament PLA Galaxy Black")]
        assert not app.orca.host.ui.windows

    def test_grey_is_gray(self, app):
        stock(app, filament("Spike PLA Space Grey", "Spike", "PLA", "#202020"),
              filament("Spike PLA Teal", "Spike", "PLA", "#008080"))
        load(app, app.printer.tool("PLA", "Spike", "Gray", "#808080"))
        app.match()
        assert selected(app) == [(1, "Spike PLA Space Grey")]

    def test_a_name_with_another_printer_color_is_another_color(self, app):
        """Its color value may be close, and still the printer would show another color's name."""
        stock(app, filament("Spike PLA Silver", "Spike", "PLA", "#F0F0F0"),
              filament("Spike PLA Arctic", "Spike", "PLA", "#E0E8F0"))
        load(app, app.printer.tool("PLA", "Spike", "White", "#FFFFFF"))
        app.match()
        assert selected(app) == [(1, "Spike PLA Arctic")]

    def test_a_color_named_otherwise_goes_by_its_value(self, app):
        """Navy is none of the printer's colors, so a preset named Dark Blue may be it."""
        stock(app, filament("Spike PLA Dark Blue", "Spike", "PLA", "#000080"),
              filament("Spike PLA", "Spike", "PLA", None),
              filament("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        load(app, app.printer.tool("PLA", "Spike", "Navy", "#000080"))
        app.match()
        assert selected(app) == [(1, "Spike PLA Dark Blue")]
        assert app.dialogs[-1].endswith(f"Slot 1: Spike PLA Dark Blue, in place of {SYSTEM}")

    def test_a_color_named_otherwise_and_far_off_is_another_color(self, app):
        stock(app, filament("Spike PLA", "Spike", "PLA", None), filament("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        load(app, app.printer.tool("PLA", "Spike", "Navy", "#000080"))
        app.match()
        assert selected(app) == [(1, "Spike PLA")]
        assert app.dialogs[-1].endswith("(no installed Spike PLA preset is Navy)")

    def test_shades_the_printer_names_alike_are_one_color(self, app):
        """White and off-white are both White to the printer, so both are offered, the plainer name first."""
        stock(app, filament("Spike PLA Silk Ivory", "Spike", "PLA", "#FFFFFF"),
              filament("Spike PLA Ivory", "Spike", "PLA", "#F5F5DC"))
        load(app, app.printer.tool("PLA", "Spike", "", "#FFFFFF"))
        app.match()
        assert selected(app) == [(1, "Spike PLA Ivory")]
        assert labels(picker_rows(app)[0]) == ["Spike PLA Ivory", "Spike PLA Silk Ivory"]

    def test_the_closest_color_breaks_a_tie(self, app):
        stock(app, filament("Spike PLA Snow", "Spike", "PLA", "#F0F0F0"),
              filament("Spike PLA Cloud", "Spike", "PLA", "#FAFAFA"))
        load(app, app.printer.tool("PLA", "Spike", "", "#FFFFFF"))
        app.match()
        assert selected(app) == [(1, "Spike PLA Cloud")]

    @pytest.mark.parametrize("color", [None, "", "#808080"])
    def test_a_filament_without_a_color_comes_before_other_colors(self, app, color):
        """A preset may leave its color out, and Filament DB gives a filament without one #808080."""
        stock(app, filament("Spike PLA Teal", "Spike", "PLA", "#008080"), filament("Spike PLA", "Spike", "PLA", color))
        load(app, app.printer.tool("PLA", "Spike", "Red", "#FF0000"))
        app.match()
        assert selected(app) == [(1, "Spike PLA")]
        assert app.dialogs[-1].endswith("(no installed Spike PLA preset is Red)")

    def test_a_gray_filament_keeps_its_color(self, app):
        stock(app, filament("Spike PLA", "Spike", "PLA", None), filament("Spike PLA Gray", "Spike", "PLA", "#808080"))
        load(app, app.printer.tool("PLA", "Spike", "", "#808080"))
        app.match()
        assert selected(app) == [(1, "Spike PLA Gray")]

    def test_only_other_colors_selects_the_closest_and_says_so(self, app):
        stock(app, filament("Spike PLA Navy", "Spike", "PLA", "#000080"),
              filament("Spike PLA Coral", "Spike", "PLA", "#FF7F50"))
        load(app, app.printer.tool("PLA", "Spike", "", "#C80000"))
        app.match()
        assert selected(app) == [(1, "Spike PLA Coral")]
        [row] = picker_rows(app)
        assert labels(row) == ["Spike PLA Coral", "Spike PLA Navy"]
        assert row["detail"] == f"Now {SYSTEM}; no installed Spike PLA preset is #C80000"

    def test_a_tool_without_a_color_counts_against_no_preset(self, app):
        stock(app, filament("Overture PLA Silk", "Overture", "PLA", "#FF0000"),
              filament("Overture PLA", "Overture", "PLA", "#0000FF"))
        load(app, app.printer.tool("PLA", "Overture", "", ""))
        app.match()
        assert selected(app) == [(1, "Overture PLA")]
        [row] = picker_rows(app)
        assert labels(row) == ["Overture PLA", "Overture PLA Silk"]
        assert (row["color"], row["detail"]) == ("", f"Now {SYSTEM}")

    def test_the_fewest_extra_words_win(self, app):
        stock(app, filament("Overture PLA Silk White", "Overture", "PLA", "#FFFFFF"),
              filament("Overture PLA White", "Overture", "PLA", "#FFFFFF"))
        load(app, app.printer.tool("PLA", "Overture", "White", "#FFFFFF"))
        app.match()
        assert selected(app) == [(1, "Overture PLA White")]

    @pytest.mark.parametrize("first, second", [((0, 0, 0), (255, 255, 255)), ((255, 0, 0), (255, 0, 0))])
    def test_color_difference(self, plugin, first, second):
        difference = plugin.color_difference(first, second)
        assert difference == pytest.approx(100 if first != second else 0, abs=0.01)


class TestAbrasive:
    def stock_two(self, app):
        stock(app, filament("Spike PETG Carbon Fiber Black", "Spike", "PETG", "#000000"),
              filament("Spike PETG Red", "Spike", "PETG", "#FF0000"))

    def test_a_soft_nozzle_gets_abrasive_filament_last(self, app):
        self.stock_two(app)
        load(app, app.printer.tool("PETG", "Spike", "Black", "#000000", hardened=False))
        app.match()
        assert selected(app) == [(1, "Spike PETG Red")]
        assert app.dialogs[-1].endswith(
            "(Spike PETG Carbon Fiber Black is Black, but abrasive, and the nozzle is not hardened)")

    def test_an_abrasive_preset_of_another_color_is_not_noted(self, app):
        stock(app, filament("Spike PETG CF Red", "Spike", "PETG", "#FF0000"),
              filament("Spike PETG Black", "Spike", "PETG", "#000000"))
        load(app, app.printer.tool("PETG", "Spike", "Black", "#000000"))
        app.match()
        assert app.dialogs[-1].endswith(f"Slot 1: Spike PETG Black, in place of {SYSTEM}")

    @pytest.mark.parametrize("text", ["PA-CF", "PETG-CF10", "GF30", "PA6 Glass Fibre", "PETG Carbon Fiber Black",
                                      "PLA CarbonFiber", "PETG Carbon", "Carbon PLA"])
    def test_names_of_fiber_filled_filament(self, plugin, text):
        assert plugin.ABRASIVE_WORDS.search(text)

    @pytest.mark.parametrize("text", ["Polycarbonate", "PC Blend", "PLA Carbon Black", "PLA Carbon Grey",
                                      "PLA Carbon-Gray", "Cfx PLA", "Golf Green"])
    def test_names_of_other_filament(self, plugin, text):
        assert not plugin.ABRASIVE_WORDS.search(text)

    def test_polycarbonate_is_not_abrasive(self, app):
        stock(app, filament("Spike Polycarbonate Black", "Spike", "PC", "#000000"),
              filament("Spike PC Gray", "Spike", "PC", "#808080"))
        load(app, app.printer.tool("PC", "Spike", "Black", "#000000"))
        app.match()
        assert selected(app) == [(1, "Spike Polycarbonate Black")]

    def test_a_hardened_nozzle_takes_it(self, app):
        self.stock_two(app)
        load(app, app.printer.tool("PETG", "Spike", "Black", "#000000", hardened=True))
        app.match()
        assert selected(app) == [(1, "Spike PETG Carbon Fiber Black")]

    @pytest.mark.parametrize("record", [
        filament("Spike PA", "Spike", "PA-GF", "#000000"),
        filament("Spike PA", "Spike", "PA", "#000000", hrc="40"),
        filament("Spike PA Glass Fibre", "Spike", "PA", "#000000"),
        filament("My PA", "Spike", "PA", "#000000", parent="Spike PA-CF @CORE One"),
    ])
    def test_the_only_match_says_it_is_abrasive(self, app, record):
        stock(app, record)
        material = record.values["filament_type"]
        load(app, app.printer.tool(material, "Spike", "Black", "#000000"))
        app.match()
        assert selected(app) == [(1, record.name)]
        assert app.dialogs[-1].endswith("(it is abrasive, and the nozzle is not hardened)")

    def test_a_printer_name_is_not_read_for_fiber(self, app):
        """CF and GF in the printer part of a name, as in a printer called GF1, say nothing about the filament."""
        stock(app, filament("Spike PETG Black @GF1", "Spike", "PETG", "#000000", alias="Spike PETG Black", is_system=True))
        load(app, app.printer.tool("PETG", "Spike", "Black", "#000000"))
        app.match()
        assert app.dialogs[-1].endswith(f"Slot 1: Spike PETG Black, in place of {SYSTEM}")


class TestKeeping:
    def test_keeps_the_slots_preset_when_it_fits_as_well(self, app):
        """The printer cannot tell matte from plain, so the user's pick stays."""
        stock(app, filament("Overture PLA White", "Overture", "PLA", "#FFFFFF"),
              filament("Overture PLA Matte White", "Overture", "PLA", "#FAFAFA"))
        slots(app, "Overture PLA Matte White")
        load(app, app.printer.tool("PLA", "Overture", "White", "#FFFFFF"))
        result = app.match()
        assert result.message == "Nothing to change.\n\nSlot 1: keeps Overture PLA Matte White"
        assert not app.dialogs and not app.editor.selections
        assert labels(picker_rows(app)[0]) == ["Overture PLA Matte White", "Overture PLA White"]

    def test_replaces_a_preset_of_another_color(self, app):
        stock(app, filament("Overture PLA White", "Overture", "PLA", "#FFFFFF"),
              filament("Overture PLA Black", "Overture", "PLA", "#000000"))
        slots(app, "Overture PLA Black")
        load(app, app.printer.tool("PLA", "Overture", "White", "#FFFFFF"))
        app.match()
        assert app.dialogs[-1].endswith("Slot 1: Overture PLA White, in place of Overture PLA Black")
        assert selected(app) == [(1, "Overture PLA White")]

    def test_keeps_a_preset_without_the_color_in_its_name(self, app):
        stock(app, filament("Overture PLA White", "Overture", "PLA", "#FFFFFF"),
              filament("Overture PLA Tuned", "Overture", "PLA", "#FFFFFF"))
        slots(app, "Overture PLA Tuned")
        load(app, app.printer.tool("PLA", "Overture", "White", "#FFFFFF"))
        assert app.match().message == "Nothing to change.\n\nSlot 1: keeps Overture PLA Tuned"
        assert labels(picker_rows(app)[0]) == ["Overture PLA Tuned", "Overture PLA White"]

    def test_a_preset_counts_as_the_one_it_inherits_from(self, app):
        """Without that, a preset named for something else loses to its parent for a color named on the printer."""
        stock(app, filament("Spike PLA Galaxy Black @CORE One", "Spike", "PLA", "#3D3E3D",
                            alias="Spike PLA Galaxy Black", is_system=True),
              filament("My Tuned PLA", "Spike", "PLA", "#3D3E3D", parent="Spike PLA Galaxy Black @CORE One"))
        slots(app, "My Tuned PLA")
        load(app, app.printer.tool("PLA", "Spike", "Galaxy Black", "#101010"))
        assert app.match().message == "Nothing to change.\n\nSlot 1: keeps My Tuned PLA"
        assert labels(picker_rows(app)[0]) == ["My Tuned PLA", "Spike PLA Galaxy Black"]

    def test_keeps_a_preset_the_dropdown_no_longer_offers(self, app):
        stock(app, filament("Overture PLA White", "Overture", "PLA", "#FFFFFF"),
              filament("My PLA", "Overture", "PLA", "#FFFFFF", is_visible=False))
        slots(app, "My PLA")
        load(app, app.printer.tool("PLA", "Overture", "White", "#FFFFFF"))
        assert app.match().message == "Nothing to change.\n\nSlot 1: keeps My PLA"

    def test_a_preset_of_another_vendor_is_replaced(self, app):
        stock(app, filament("Spike PLA", "Spike", "PLA", None))
        load(app, app.printer.tool("PLA", "Spike", "", ""))
        app.match()
        assert selected(app) == [(1, "Spike PLA")]

    def test_a_slots_preset_with_unsaved_edits_shows_its_name(self, app):
        app.filaments.selected.is_dirty = True
        stock(app, filament("Spike PLA", "Spike", "PLA", None))
        load(app, app.printer.tool("PLA", "Spike", "", ""))
        app.match()
        assert app.dialogs[-1].endswith(f"Slot 1: Spike PLA, in place of {SYSTEM}")

    def test_a_slots_preset_that_is_gone_shows_its_name(self, app):
        stock(app, filament("Spike PLA", "Spike", "PLA", None))
        slots(app, "Removed PLA")
        load(app, app.printer.tool("PLA", "Spike", "", ""))
        app.match()
        assert app.dialogs[-1].endswith("Slot 1: Spike PLA, in place of Removed PLA")


class TestPicking:
    """Presets that fit a tool as well differ in what the printer cannot report, so the user picks."""

    def stock_whites(self, app, *more):
        stock(app, filament("Pro PCTG Snow White", "3D Fuel", "PCTG", "#EEF2EF"),
              filament("Pro PCTG Translucent White", "3D Fuel", "PCTG", "#D5D9DC"),
              filament("Pro PCTG Natural", "3D Fuel", "PCTG", "#A9A9A6"), *more)

    def test_offers_every_preset_named_for_the_tools_color(self, app):
        self.stock_whites(app)
        load(app, app.printer.tool("PCTG", "3D-Fuel", "White", "#FFFFFF"))
        app.orca.host.ui.picks = lambda rows: [1]
        result = app.match()
        [window] = app.orca.host.ui.windows
        assert window.title == "Match printer filaments"
        assert window.data == {"rows": [{
            "slot": 1, "tool": "3D-Fuel PCTG White", "color": "#FFFFFF", "detail": f"Now {SYSTEM}",
            "options": [{"label": "Pro PCTG Snow White", "color": "#EEF2EF"},
                        {"label": "Pro PCTG Translucent White", "color": "#D5D9DC"}],
        }], "notes": ""}
        assert selected(app) == [(1, "Pro PCTG Translucent White")]
        assert result.message == "Selected filament presets for slot 1."
        # The picker asks instead of the confirmation.
        assert not app.dialogs

    def test_offers_them_best_first(self, app):
        stock(app, *(filament(f"Spike PLA {name}", "Spike", "PLA", color) for name, color in [
            ("Pearl", "#EAE0C8"), ("Arctic", "#E0E8F0"), ("Silk White", "#FFFFFF"), ("White", "#FFFFFF"),
            ("Ivory", "#F5F5DC"), ("Red", "#FF0000")]))
        load(app, app.printer.tool("PLA", "Spike", "White", "#FFFFFF"))
        app.match()
        assert labels(picker_rows(app)[0]) == [
            "Spike PLA White", "Spike PLA Silk White", "Spike PLA Arctic", "Spike PLA Ivory", "Spike PLA Pearl"]

    def test_a_close_shade_in_the_slot_stays_for_one_of_the_printers_colors(self, app):
        """The printer has a few color names, so its White may be the user's Ivory."""
        stock(app, filament("Spike PLA White", "Spike", "PLA", "#FFFFFF"), filament("Spike PLA Ivory", "Spike", "PLA", "#F5F5DC"))
        slots(app, "Spike PLA Ivory")
        load(app, app.printer.tool("PLA", "Spike", "White", "#FFFFFF"))
        assert app.match().message == "Nothing to change.\n\nSlot 1: keeps Spike PLA Ivory"
        assert labels(picker_rows(app)[0]) == ["Spike PLA Ivory", "Spike PLA White"]

    def test_a_color_named_on_the_printer_is_selected_alone(self, app):
        """A color the user named on the printer, such as Galaxy Black, says which filament it is."""
        stock(app, filament("Spike PLA Galaxy Black", "Spike", "PLA", "#3D3E3D"),
              filament("Spike PLA Jet", "Spike", "PLA", "#000000"))
        slots(app, "Spike PLA Jet")
        load(app, app.printer.tool("PLA", "Spike", "Galaxy Black", "#101010"))
        app.match()
        assert not app.orca.host.ui.windows
        assert app.dialogs[-1].endswith("Slot 1: Spike PLA Galaxy Black, in place of Spike PLA Jet")
        assert selected(app) == [(1, "Spike PLA Galaxy Black")]

    def test_abrasive_filament_is_not_offered_for_a_soft_nozzle(self, app):
        stock(app, filament("Spike PETG Black", "Spike", "PETG", "#000000"),
              filament("Spike PETG CF Black", "Spike", "PETG", "#000000"))
        load(app, app.printer.tool("PETG", "Spike", "Black", "#000000", hardened=False))
        app.match()
        assert not app.orca.host.ui.windows
        assert selected(app) == [(1, "Spike PETG Black")]

    def test_the_slots_preset_comes_first_and_can_be_changed(self, app):
        self.stock_whites(app)
        slots(app, "Pro PCTG Snow White")
        load(app, app.printer.tool("PCTG", "3D Fuel", "White", "#FFFFFF"))
        app.orca.host.ui.picks = lambda rows: [1]
        app.match()
        assert labels(picker_rows(app)[0]) == ["Pro PCTG Snow White", "Pro PCTG Translucent White"]
        assert selected(app) == [(1, "Pro PCTG Translucent White")]

    def test_shows_every_slot_and_what_it_leaves_out(self, app):
        self.stock_whites(app, filament("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        slots(app, SYSTEM_NAME, SYSTEM_NAME, SYSTEM_NAME)
        load(app, app.printer.tool("PCTG", "3D Fuel", "White", "#FFFFFF"), app.printer.tool("PLA", "Spike", "Red", "#FF0000"),
             app.printer.tool("PETG", "Spike", "Blue", "#0000FF"), app.printer.tool())
        app.match()
        [window] = app.orca.host.ui.windows
        assert [(row["slot"], len(row["options"])) for row in window.data["rows"]] == [(1, 2), (2, 1)]
        assert window.data["notes"] == ("Slot 3: left alone, as no installed preset is Spike PETG\n"
                                        "Empty on the printer, so left alone: tool 4")
        assert window.size == (680, 160 + 2 * 62 + 2 * 18)
        assert selected(app) == [(1, "Pro PCTG Snow White"), (2, "Spike PLA Red")]

    def test_cancel_changes_nothing(self, app):
        self.stock_whites(app)
        load(app, app.printer.tool("PCTG", "3D Fuel", "White", "#FFFFFF"))
        app.orca.host.ui.picks = lambda rows: None
        result = app.match()
        assert (result.status, result.message) == (PluginResult.Skipped, "Nothing was changed.")
        assert not app.editor.selections

    def test_waits_while_the_user_picks(self, app):
        self.stock_whites(app)
        load(app, app.printer.tool("PCTG", "3D Fuel", "White", "#FFFFFF"))
        app.orca.host.ui.timing = "later"
        app.match()
        assert selected(app) == [(1, "Pro PCTG Snow White")]

    def test_a_dialog_closed_without_an_answer_changes_nothing(self, app):
        """Unloading the plugin closes its windows without calling back."""
        self.stock_whites(app)
        load(app, app.printer.tool("PCTG", "3D Fuel", "White", "#FFFFFF"))
        app.orca.host.ui.timing = "unloaded"
        assert app.match().message == "Nothing was changed."
        assert not app.editor.selections

    @pytest.mark.parametrize("answer", ["yes", {"picks": "0"}, [0, 0], [2], [-1], ["0"], [True]])
    def test_an_answer_it_cannot_read_changes_nothing(self, app, answer):
        self.stock_whites(app)
        load(app, app.printer.tool("PCTG", "3D Fuel", "White", "#FFFFFF"))
        app.orca.host.ui.picks = lambda rows: answer
        assert app.match().message == "Nothing was changed."
        assert not app.editor.selections

    def test_a_label_cannot_end_the_pages_script(self, plugin, orca):
        tool = plugin.Tool(1, "PLA", "Spike", "White", (255, 255, 255), 0.4, False)
        presets = [plugin.Candidate("a", "</script><b>", "Spike", "PLA", None, False, frozenset()),
                   plugin.Candidate("b", "Spike PLA", "Spike", "PLA", None, False, frozenset())]
        step = plugin.Step(tool, "Old", presets, False)
        orca.host.ui.picks = lambda rows: [1]
        assert plugin.pick([step], [0.4], []) == [(step, presets[1])]
        [window] = orca.host.ui.windows
        assert window.html.count("</script>") == 2
        assert window.data["rows"][0]["options"][0]["label"] == "</script><b>"

    @pytest.mark.parametrize("was, height", [("Old", 160 + 62), ("x" * 120, 160 + 62 + 2 * 18)])
    def test_a_long_detail_makes_room_for_its_lines(self, plugin, orca, was, height):
        """The rows scroll when they do not fit, but Select and Cancel stay in view."""
        tool = plugin.Tool(1, "PLA", "Spike", "", None, 0.4, False)
        presets = [plugin.candidate(f"p{n}", f"Spike PLA {n}", {}) for n in range(2)]
        orca.host.ui.picks = lambda rows: [0]
        plugin.pick([plugin.Step(tool, was, presets, False)], [0.4], [])
        [window] = orca.host.ui.windows
        assert window.size == (plugin.PICKER_WIDTH, height)
        assert window.html.index('id="content"') < window.html.index('id="rows"') < window.html.index('id="buttons"')


class TestSlots:
    def stock_three(self, app):
        stock(app, filament("Spike PLA Red", "Spike", "PLA", "#FF0000"),
              filament("Spike PETG Blue", "Spike", "PETG", "#0000FF"),
              filament("Spike ASA Black", "Spike", "ASA", "#000000"))

    def test_tool_n_is_slot_n_and_the_rest_are_listed(self, app):
        self.stock_three(app)
        slots(app, SYSTEM_NAME, SYSTEM_NAME, SYSTEM_NAME)
        load(app, app.printer.tool("PLA", "Spike", "Red", "#FF0000"), app.printer.tool(),
             app.printer.tool("PETG", "Spike", "Blue", "#0000FF"), None,
             app.printer.tool("ASA", "Spike", "Black", "#000000"), app.printer.tool("PLA", "Spike", "Red", "#FF0000"))
        app.printer.tools["9"] = {"loaded": "yes"}
        result = app.match()
        assert app.dialogs == ["Select these filament presets to match the printer?\n\n"
                               f"Slot 1: Spike PLA Red, in place of {SYSTEM}\n"
                               f"Slot 3: Spike PETG Blue, in place of {SYSTEM}\n"
                               "Empty on the printer, so left alone: tool 2\n"
                               "Not in this project, which has 3 filament slots: tools 5 and 6\n"
                               "Could not read what the printer sent for tool 9"]
        assert selected(app) == [(1, "Spike PLA Red"), (3, "Spike PETG Blue")]
        assert result.message == "Selected filament presets for slots 1 and 3."

    def test_a_project_with_one_slot(self, app):
        self.stock_three(app)
        load(app, app.printer.tool(), app.printer.tool(), app.printer.tool("PETG", "Spike", "Blue", "#0000FF"))
        result = app.match()
        assert result.message == ("Nothing to change.\n\n"
                                  "Empty on the printer, so left alone: tools 1 and 2\n"
                                  "Not in this project, which has 1 filament slot: tool 3")

    def test_lists_three_numbers_with_commas(self, plugin):
        assert plugin.numbered("tool", [4, 6, 8]) == "tools 4, 6 and 8"

    def test_cancel_changes_nothing(self, app):
        self.stock_three(app)
        load(app, app.printer.tool("PLA", "Spike", "Red", "#FF0000"))
        app.orca.host.ui.answer = "cancel"
        result = app.match()
        assert (result.status, result.message) == (PluginResult.Skipped, "Nothing was changed.")
        assert not app.editor.selections

    def test_a_cancelled_switch_stops_the_rest(self, app):
        """OrcaSlicer asks about unsaved edits before it switches, and the user may cancel."""
        self.stock_three(app)
        slots(app, SYSTEM_NAME, SYSTEM_NAME, SYSTEM_NAME)
        load(app, app.printer.tool("PLA", "Spike", "Red", "#FF0000"), app.printer.tool("PETG", "Spike", "Blue", "#0000FF"),
             app.printer.tool("ASA", "Spike", "Black", "#000000"))
        app.editor.cancelled_slots.add(2)
        result = app.match()
        assert result.status is PluginResult.Skipped
        assert result.message == ('Stopped at slot 2: Switching the edited filament preset to "Spike PETG Blue" '
                                  "was canceled. Slots changed before it: 1.")
        assert selected(app) == [(1, "Spike PLA Red")]

    def test_a_cancelled_first_switch_changes_nothing(self, app):
        self.stock_three(app)
        load(app, app.printer.tool("PLA", "Spike", "Red", "#FF0000"))
        app.editor.cancelled_slots.add(1)
        assert app.match().message.endswith("was canceled.")

    def test_a_refused_slot_is_reported_and_the_rest_go_ahead(self, app):
        self.stock_three(app)
        slots(app, SYSTEM_NAME, SYSTEM_NAME)
        load(app, app.printer.tool("PLA", "Spike", "Red", "#FF0000"), app.printer.tool("PETG", "Spike", "Blue", "#0000FF"))
        app.editor.mixed_color_slots.add(1)
        result = app.match()
        assert result.message == ("Selected filament presets for slot 2.\n"
                                  "OrcaSlicer would not select Spike PLA Red for slot 1: "
                                  "Filament slot 1 is a mixed-color virtual slot")

    def test_only_refused_slots(self, app):
        self.stock_three(app)
        load(app, app.printer.tool("PLA", "Spike", "Red", "#FF0000"))
        app.editor.mixed_color_slots.add(1)
        result = app.match()
        assert result.status is PluginResult.Skipped
        assert result.message == ("OrcaSlicer would not select Spike PLA Red for slot 1: "
                                  "Filament slot 1 is a mixed-color virtual slot\nNothing was changed.")
        assert app.notifications[-1][0] is NotificationLevel.WarningNotificationLevel

    def test_an_ampersand_shows_in_the_confirmation(self, app):
        """OrcaSlicer's message dialog would take a lone & for a shortcut key and drop it."""
        stock(app, filament("R&D PLA Red", "R&D", "PLA", "#FF0000"))
        load(app, app.printer.tool("PLA", "R&D", "Red", "#FF0000"))
        app.match()
        assert app.dialogs[-1].endswith(f"Slot 1: R&&D PLA Red, in place of {SYSTEM}")
        assert selected(app) == [(1, "R&D PLA Red")]


class TestNozzles:
    def test_a_nozzle_unlike_the_printer_profiles_is_noted(self, app):
        """OrcaSlicer slices for the profile's nozzle, so the profile may not suit this tool."""
        stock(app, filament("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        slots(app, SYSTEM_NAME, SYSTEM_NAME)
        app.state.nozzle = "0.4,0.6"
        load(app, app.printer.tool("PLA", "Spike", "Red", "#FF0000", nozzle=0.4),
             app.printer.tool("PLA", "Spike", "Red", "#FF0000", nozzle=0.4))
        app.match()
        assert app.dialogs[-1].endswith(f"Slot 2: Spike PLA Red, in place of {SYSTEM} "
                                        "(the tool has a 0.4 mm nozzle, and the printer profile 0.6 mm)")
        assert f"Slot 1: Spike PLA Red, in place of {SYSTEM}\n" in app.dialogs[-1]

    def test_a_tool_beyond_the_profiles_nozzles_takes_the_last(self, app):
        stock(app, filament("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        slots(app, SYSTEM_NAME, SYSTEM_NAME)
        load(app, None, app.printer.tool("PLA", "Spike", "Red", "#FF0000", nozzle=0.6))
        app.match()
        assert app.dialogs[-1].endswith("(the tool has a 0.6 mm nozzle, and the printer profile 0.4 mm)")

    @pytest.mark.parametrize("profile, tool", [(None, 0.6), ("0.4", None)])
    def test_an_unknown_nozzle_is_not_compared(self, app, profile, tool):
        stock(app, filament("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        app.state.nozzle = profile
        load(app, app.printer.tool("PLA", "Spike", "Red", "#FF0000", nozzle=tool))
        app.match()
        assert app.dialogs[-1].endswith(f"in place of {SYSTEM}")


class TestRunning:
    def test_a_second_run_waits_its_turn(self, app):
        """OrcaSlicer starts a thread per click, and two runs would race to switch the slots."""
        assert app.plugin.RUN_LOCK.acquire(blocking=False)
        result = app.match()
        app.plugin.RUN_LOCK.release()
        assert (result.status, result.message) == (PluginResult.Skipped, "Match printer filaments is still running.")
        assert not app.printer.requests

    def test_a_host_failure_is_reported_instead_of_raised(self, app, orca):
        """OrcaSlicer disables a plugin whose script raises, which removes its command."""
        def broken():
            raise RuntimeError("Preset bundle is not available")
        orca.host.preset_bundle = broken
        result = app.match()
        assert (result.status, result.message) == (PluginResult.Skipped,
                                                   "Match printer filaments failed: Preset bundle is not available")

    @pytest.mark.parametrize("value, expected", [
        ("0.97,0.98", ["0.97", "0.98"]),
        ("#F7ADA6", ["#F7ADA6"]),
        ('"Bambu Lab"', ["Bambu Lab"]),
        ('"one";"two"', ["one", "two"]),
        ('"a\\nb\\t\\"c\\\\"', ['a\nb\t"c\\']),
        ('"unterminated', []),
    ])
    def test_decode_value(self, plugin, value, expected):
        assert plugin.decode_value(value) == expected

    def test_registers_the_command(self, plugin, orca):
        plugin.PrusaLinkFilamentMatch().register_capabilities()
        assert [capability().get_name() for capability in orca.registered] == ["Match printer filaments"]
        assert plugin.MatchPrinterFilaments().get_default_config() == {
            "address": "", "api_key": "", "user": "", "password": ""}
