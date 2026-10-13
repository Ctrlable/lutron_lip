"""A wrong integration login must be said as one, at setup and after.

WHY (2026-10-13, 9360-balada) A new install failed with
  LIPProtocolError: received=[bad ] expected=[NET> ]
The processor had refused the telnet login ("bad login"), but the setup form
only downloads the system data -- which needs no login -- so it accepted the
wrong password, and the entry then failed with a protocol error nobody could
act on. Executed here against a fake processor on a real socket:

  1. the protocol: a refused login raises LIPLoginError and leaves the
     connection retryable (it used to stay CONNECTING); a good one connects;
  2. the setup form's login test: invalid_auth / None / cannot_connect;
  3. setup of an existing entry: a refused login is ConfigEntryAuthFailed
     (Ctrlable Pro's re-enter-credentials prompt), an unreachable processor
     is ConfigEntryNotReady (retried), not a hard error.

Needs Home Assistant (run inside the HA container):
  python3 tests/test_login.py <path to custom_components>
"""
import asyncio
import importlib
import sys
import types
from pathlib import Path

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[1] / "custom_components"))
protocol = importlib.import_module("lutron_lip.aiolip.protocol")
LIP = importlib.import_module("lutron_lip")
cf = importlib.import_module("lutron_lip.config_flow")
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady  # noqa: E402

fails = []


def check(ok, why):
    if not ok:
        fails.append(why)


async def processor(reader, writer):
    """RadioRA 2 main repeater, as far as a login goes."""
    try:
        writer.write(b"login: ")
        user = (await reader.readline()).strip()
        writer.write(b"password: ")
        pwd = (await reader.readline()).strip()
        if (user, pwd) != (b"lutron", b"integration"):
            writer.write(b"bad login\r\nlogin: ")
            await writer.drain()
            await reader.read(100)
            return
        writer.write(b"GNET> ")
        while line := await reader.readline():
            writer.write(b"GNET> ")
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


class Hass:
    def __init__(self, loop):
        self.loop = loop
        self.data = {}
        self.config = types.SimpleNamespace(path=lambda *p: "/tmp/lutron_test_" + "_".join(p))

    async def async_add_executor_job(self, fn, *a):
        return fn(*a)


async def main():
    server = await asyncio.start_server(processor, "127.0.0.1", 0)
    protocol.LIP_PORT = server.sockets[0].getsockname()[1]
    hass = Hass(asyncio.get_running_loop())

    print("1. protocol")
    lip = protocol.LIP()
    try:
        await lip.async_connect("127.0.0.1", "lutron", "wrong")
        check(False, "a refused login connected")
    except Exception as err:  # noqa: BLE001
        check(type(err).__name__ == "LIPLoginError", f"refused login raised {type(err).__name__}: {err}")
    check(lip.connection_state == protocol.LIPConnectionState.NOT_CONNECTED,
          f"a failed login leaves the connection retryable, got {lip.connection_state}")
    try:
        await lip.async_connect("127.0.0.1", "lutron", "integration")
        check(lip.connection_state == protocol.LIPConnectionState.CONNECTED, "the right login connects")
    except Exception as err:  # noqa: BLE001
        check(False, f"retry with the right login failed: {type(err).__name__}: {err}")
    await lip.async_stop()

    print("2. the setup form tests the login")
    try_login = getattr(cf, "async_try_login", None)
    check(try_login is not None, "the setup form never tests the login")
    if try_login:
        check(await try_login(hass, "127.0.0.1", "lutron", "wrong") == "invalid_auth", "wrong password -> invalid_auth")
        check(await try_login(hass, "127.0.0.1", "lutron", "integration") is None, "right login -> no error")
        port = protocol.LIP_PORT
        protocol.LIP_PORT = 9   # discard: nothing listens
        check(await try_login(hass, "127.0.0.1", "lutron", "integration") == "cannot_connect", "unreachable -> cannot_connect")
        protocol.LIP_PORT = port

    print("3. setup of an existing entry")
    LIP.LutronController.load_xml_db = lambda self, *a, **k: None
    for pwd, want in (("wrong", ConfigEntryAuthFailed), ("integration-unreachable", ConfigEntryNotReady)):
        if pwd.endswith("unreachable"):
            protocol.LIP_PORT, saved = 9, protocol.LIP_PORT
        entry = types.SimpleNamespace(entry_id="e1", options={},
                                      data={"host": "127.0.0.1", "username": "lutron", "password": pwd})
        try:
            await LIP.async_setup_entry(hass, entry)
            check(False, f"{pwd}: setup went on")
        except want:
            pass
        except Exception as err:  # noqa: BLE001
            check(False, f"{pwd}: setup raised {type(err).__name__}: {err}, want {want.__name__}")
        if pwd.endswith("unreachable"):
            protocol.LIP_PORT = saved
    server.close()


asyncio.run(main())
if fails:
    for f in fails:
        print("FAIL:", f)
    sys.exit(1)
print("OK")
