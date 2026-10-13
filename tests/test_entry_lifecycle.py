"""Keypad parenting, and what a reload does to the LIP session.

WHY (2026-10-13) Three gaps found while checking why areas "came back":
  1. keypad entities set DeviceInfo `via_device` directly. Ctrlable Pro 2026.9+
     rejects that inside an entity's DeviceInfo (it is applied by core, where no
     frame of ours is on the stack), and every start logged it;
  2. changing an option (Suggest areas, naming) did nothing until a restart --
     there was no update listener;
  3. unloading an entry left the LIP session open, so a reload would have run
     two logins on the processor side by side.

Needs Home Assistant itself (run it where HA is installed, e.g. inside the HA
container):  python3 tests/test_entry_lifecycle.py <path to custom_components>
"""
import asyncio
import importlib
import sys
import types
from pathlib import Path

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[1] / "custom_components"))
LIP = importlib.import_module("lutron_lip")
entity = importlib.import_module("lutron_lip.entity")
const = importlib.import_module("lutron_lip.const")

fails = []


def check(ok, why):
    if not ok:
        fails.append(why)


print("1. keypad devices are parented by registry id, never via_device")
area = types.SimpleNamespace(name="Kitchen", location="Main")
keypad = types.SimpleNamespace(device_type="SEETOUCH_KEYPAD", serial_number="123", name="Island keypad",
                               area=area, integration_id=42)
button = types.SimpleNamespace(keypad=keypad, component_number=1, area=area, name="Button 1",
                               integration_id=42, uuid="u1", legacy_uuid="42-1")
controller = types.SimpleNamespace(guid="guid-1", ha_device_id="dev-controller", suggest_areas=False,
                                   use_radiora_mode=False, use_full_path=False, use_area_for_device_name=False)
ent = entity.LutronKeypadComponent(button, controller)
info = dict(ent._attr_device_info)
if const._SUPPORTS_VIA_DEVICE_ID:
    check("via_device" not in info, f"keypad DeviceInfo still carries via_device: {info.get('via_device')}")
    check(info.get("via_device_id") == "dev-controller", f"keypad not parented by registry id: {info}")
check(info.get("suggested_area") is None, "Suggest areas off must suggest no area")

print("2. a changed option reloads the entry")
reloaded = []
hass = types.SimpleNamespace(data={}, config_entries=types.SimpleNamespace())


async def _reload(entry_id):
    reloaded.append(entry_id)
hass.config_entries.async_reload = _reload
listener = getattr(LIP, "_async_options_updated", None)
check(listener is not None, "no options update listener")
if listener:
    asyncio.run(listener(hass, types.SimpleNamespace(entry_id="e1")))
    check(reloaded == ["e1"], f"options change did not reload: {reloaded}")

print("3. unloading closes the LIP session")
stopped = []


class FakeLip:
    async def async_stop(self):
        stopped.append(True)


async def _unload_platforms(entry, platforms):
    return True
hass.config_entries.async_unload_platforms = _unload_platforms
hass.data = {const.DOMAIN: {"e1": types.SimpleNamespace(controller=types.SimpleNamespace(lip=FakeLip()))}}
ok = asyncio.run(LIP.async_unload_entry(hass, types.SimpleNamespace(entry_id="e1")))
check(ok is True, "unload reported failure")
check(stopped == [True], "unload left the LIP session running")
check("e1" not in hass.data[const.DOMAIN], "unload left the entry's data behind")

if fails:
    for f in fails:
        print("FAIL:", f)
    sys.exit(1)
print("OK")
