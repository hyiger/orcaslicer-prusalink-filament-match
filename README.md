# PrusaLink Filament Match for OrcaSlicer

Reads what each tool of a Prusa printer holds over PrusaLink, and selects the installed filament preset that matches it for that tool's filament slot. Requires OrcaSlicer 2.5.0-rc or later.

## Using it

1. Install `orca_prusalink_filament_match_any.py` from OrcaSlicer's Plugins window.
2. Give it the printer's PrusaLink address and login (see [Settings](#settings)).
3. Run **Match printer filaments**.

It reads the vendor, material and color loaded in each tool of the printer, and shows the preset it picked for each filament slot. Press OK to select them. Tool 1 goes in slot 1, tool 2 in slot 2, and so on. When more than one preset fits a tool as well, such as `Snow White` and `Translucent White` for a tool the printer calls White, it shows a list for that slot instead, with its best guess first. Pick one in each list and press Select.

The first run makes OrcaSlicer ask for permission to reach the printer: allow it, then run the command again.

## What it needs

- **A printer that reports its filaments:** a Prusa printer running [Buddy firmware with Filament Slots](https://github.com/hyiger/Prusa-Firmware-Buddy), `v6.9.2-hy.3` or later. You set each tool's vendor and color on the printer, under Filament > Filament slots. Stock Prusa firmware does not report them.
- **Presets for your filaments.** Any filament preset the slot's dropdown offers can be selected: a vendor's preset, one you saved, or one from a bundle such as [Filament DB Sync](https://github.com/hyiger/orcaslicer-filament-db-plugin)'s. Presets you hid, or that are not compatible with the printer, are left out.

## Settings

These are in the Config tab of **Match printer filaments**. Leave them empty to use the printer's own connection: click **Connection** next to the printer, choose PrusaLink, and enter the printer's address and its API key, or the user and password.

| Setting | Use |
| --- | --- |
| `address` | The printer's address, such as `192.168.1.40`. Set it when you print through Prusa Connect, so the printer's own connection can stay as it is. These settings come first when it is set. |
| `api_key` | The printer's PrusaLink API key, from its Settings > Network screen. |
| `user`, `password` | The printer's PrusaLink login, if you use it instead of the API key. |

The settings are kept in a plain file in OrcaSlicer's data folder, as the printer's connection settings are.

## How a preset is chosen

For each loaded tool, among the installed presets:

1. **Vendor and material must match.** Vendor names are compared without case or punctuation, so `3D-Fuel` matches `3D Fuel`, and the printer's `Prusament` matches `Prusa Polymers`. Materials are compared without case. `FLEX`, `TPU`, `TPE` and `PEBA` count as one, and a material the printer cut to 7 characters still matches (`PPS-CF1` matches `PPS-CF10`). `PETG` and `PET` stay apart. When no preset matches, the slot is left alone, and the list says so.
2. **The slot's preset is kept** when it fits as well as the best one, even if you hid it since.
3. Otherwise, the **best fit** is selected:
   - On a nozzle that is not hardened, abrasive filament comes last: a preset that needs a hardened nozzle, or whose name or material says carbon or glass fiber, such as `PA-CF`, `GF30`, `Carbon Fiber` or `PETG Carbon`. `Polycarbonate` and the color `Carbon Black` do not count.
   - Then the color. A preset named for a color you named on the printer, such as `Galaxy Black`, comes first. Then one named for the tool's color, or whose color is close to it, as white and off-white are: the printer's own colors, such as `White`, cover close shades named otherwise, such as `Ivory`. Then one with no color, including Filament DB's `#808080` without gray in the name. Then other colors, including a preset named for another of the printer's colors, such as `Silver` for a white tool. A color you named on the printer that has none of the printer's colors in it, such as `Navy`, goes by the color value alone, so a preset named `Dark Blue` can fit it. `Gray` and `Grey` are the same.
   - Then your own presets, saved or from a bundle, before a vendor's.
   - Then the preset whose name says the least beyond vendor, material and color, so `Overture PLA White` comes before `Overture PLA Silk White`.
   - Then the closest color.

A preset is read by the name OrcaSlicer shows, without the printer it is for, so `Prusament PLA @CORE One` is `Prusament PLA`. A preset you saved is also read by the name of the one it inherits from, so `My Tuned PLA` counts as `Galaxy Black` when it was saved from `Spike PLA Galaxy Black`. Where two presets show the same name, as a vendor's presets for two nozzles may, the list shows each one's full name.

**You pick when presets fit as well.** The printer reports only vendor, material and color, so it cannot tell plain from matte, silk or carbon fiber filament, nor `Snow White` from `Translucent White`. When presets of the tool's vendor and material are equally good by the first two rules above (abrasive filament, then color), they are all offered in a list for that slot, in the order above. The slot's own preset comes first when it is one of them. The list then replaces the confirmation, and it shows every slot, so you can check the rest too.

A tool with no vendor goes by its material alone, and a `Generic` preset comes first among presets of the same color. A tool with no color counts against no preset's color. The printer's unnamed user vendors and colors, `VENDOR1` and `COLOR1` to `8`, count as no name.

The list also notes:

- **No preset in the tool's color.** The closest one is selected instead. Add a preset in that color, to have it selected next time.
- **An abrasive preset in the tool's color** that was passed over, as the nozzle is not hardened.
- **Abrasive filament** on a nozzle that is not hardened, when it is the only match.
- **No vendor** from the printer, such as for filament loaded from G-code.
- **A nozzle unlike the printer profile's.** OrcaSlicer slices for the profile's nozzles, so pick the printer profile for the nozzles that are installed.
- **Empty tools**, whose slots are left alone, and **tools beyond the project's filament slots**. Add filament slots to the project for those.

If the Filament tab has unsaved changes, OrcaSlicer asks about them before it switches presets, as when you pick one yourself. Cancel stops the command, and the slots changed before it stay changed.

## Development

```bash
python -m venv .venv
.venv/bin/pip install pytest pytest-cov
.venv/bin/python -m pytest
```

`pytest` fails below 100% line and branch coverage. Tests run against a stand-in for OrcaSlicer's `orca` module and a stand-in PrusaLink printer served over real HTTP, in `tests/conftest.py`. They hold the rules found by testing in OrcaSlicer: a slot's selector offers only presets that are shown and compatible, or the slot's own, and takes a preset by its internal name; `find_preset()` returns the edited copy of the selected preset. The printer takes an API key or the firmware's RFC 2069 Digest.

To check the PrusaLink side against what the firmware sends byte for byte, run [prusalink-mock](https://github.com/hyiger/prusalink-mock) and point the printer's connection at `http://127.0.0.1:8088`, with API key `mockKeyTest2345`, or user `maker` and the same password.

## Testing in OrcaSlicer

The unit tests cannot reach the steps below, which need the running app.

- [ ] Point a 4-slot project's printer connection at prusalink-mock, by API key, and match. OrcaSlicer asks once for permission to reach it. After you allow it and match again, the list shows slots 1 to 3 and the empty tool 4, and OK selects those presets.
- [ ] A vendor's preset, such as `Prusament PLA` on a Prusa printer, is selected for a tool that holds it, and a preset you hid is not.
- [ ] Each changed slot shows its new preset's color, in the sidebar and in the preview.
- [ ] With two presets of a tool's vendor and material named for its color, such as `Snow White` and `Translucent White` for White, match. A dialog lists both for that slot, with each other slot's preset beside it. Pick the second and press Select: the slot gets it. Match again: the list starts with it. Cancel and Escape change nothing. The dialog is readable in OrcaSlicer's dark mode.
- [ ] Match again with the user and password instead. It reads the printer the same way.
- [ ] Set the printer's connection to Prusa Connect, and put the printer's address and API key in `address` and `api_key`. Match reads the printer, and printing still goes through Prusa Connect.
- [ ] Edit the filament in slot 1, then match a printer whose tool 1 holds another filament. OrcaSlicer asks about the edit, and Cancel leaves the slot and the edit as they were.
