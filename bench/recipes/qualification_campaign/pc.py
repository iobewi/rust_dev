"""P4/C6 generic qualification support (G1): target gate and the manual ROM-loader window.

Why a window: on the Waveshare board the C6 is reached through a CP210x on connector H7 whose DTR/RTS lines are not known to be
wired to EN/IO9, so the agent's automatic loader entry cannot be relied on. Entry is then a gesture of the operator (strap low,
reset) that the recipe makes VERIFIABLE: the ROM's download-mode banner must appear on the UART after a cursor taken before the
gesture. No loader command is submitted before that, and a banner that was not seen is BLOCKED, never assumed.
"""
import pytest

from iobewi_bench.client import OperationError

from .harness import Blocked, operator_gesture
from .readonly import require_consent
from .session import PC_CHIPS

# Error codes an unreachable loader produces (api.md): nothing was written; the gesture is simply to be repeated.
LOADER_UNREACHABLE = {"serial_timeout", "serial_partial_packet", "serial_invalid_packet"}


def gate(cfg, scenario):
    """Everything decidable from the private session alone, before any evidence or command."""
    if cfg["chip"] not in PC_CHIPS:
        pytest.skip(f"BLOCKED: this recipe targets {' or '.join(sorted(PC_CHIPS))}; the session declares {cfg['chip']}")
    require_consent(cfg, scenario)


class LoaderState:
    """Whether a manual loader window was opened in this pytest run. One per run, shared by the tests (session fixture)."""
    def __init__(self):
        self.opened = False


@pytest.fixture(scope="session")
def loader_state():
    return LoaderState()


def open_loader_window(case, board, cfg, out, state):
    """Automatic entry: nothing to do (the agent drives it; recorded as an inference). Manual entry: announce the gesture and
    observe the declared ROM banner, once per run. Raises Blocked if the banner is not seen; no command is submitted before."""
    loader = cfg["loader"]
    if loader["entry"] == "auto":
        out.evidence.append("loader entry: automatic (agent-driven; the DTR/RTS wiring is an inference, not a measurement)")
        return
    if state.opened:
        out.evidence.append("loader entry: manual window opened earlier in this run; the first loader command decides whether it still holds")
        return
    cursor = board.cursor()
    case.write("loader-window.json", {"entry": "manual", "download_marker": loader["download_marker"],
                                       "marker_timeout_s": loader["marker_timeout_s"]})
    operator_gesture(case, f"MANUAL LOADER ENTRY on {cfg['board_id']}: hold the download strap (C6: GPIO9 to GND), then reset the chip; "
                           f"the ROM must print {loader['download_marker']!r}", timeout=loader["marker_timeout_s"])
    try:
        board.wait_uart(loader["download_marker"].encode(), cursor=cursor, timeout=loader["marker_timeout_s"])
    except TimeoutError as error:
        raise Blocked(f"the ROM download banner {loader['download_marker']!r} was not seen within {loader['marker_timeout_s']} s; "
                      "no command was submitted") from error
    state.opened = True
    out.evidence.append(f"loader entry: manual; ROM banner {loader['download_marker']!r} observed after the operator's gesture")


def first_command(cfg, state, action):
    """Run the first loader command of a window. In manual mode an unreachable loader is a missing precondition (BLOCKED, the window
    is forgotten so that the gesture is requested again), not a bench failure; any other error propagates."""
    try:
        return action()
    except OperationError as error:
        code = (error.operation.get("error") or {}).get("code")
        if cfg["loader"]["entry"] == "manual" and code in LOADER_UNREACHABLE:
            state.opened = False
            raise Blocked(f"the loader is not reachable ({code}); repeat the manual entry gesture") from error
        raise
