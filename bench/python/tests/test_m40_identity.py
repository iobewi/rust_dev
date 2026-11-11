"""Software checks of M40-ID and M40-PHYS-B. SIMULATION ONLY: RAM simulated cards ('board', 'board2'), no ESP32, no Web Serial.

They prove the fingerprint policy (HMAC-SHA-256 over a versioned canonical form, private key, atomic never-overwritten reference, no raw MAC
in any persisted proof), the recipes' verdicts (PASS / FAIL / BLOCKED / INDETERMINATE) and that nothing is ever replayed or recovered
automatically. They prove nothing about real cards, real MACs or the physical wiring."""
import hashlib
import hmac
import json
import os
import selectors
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from iobewi_bench import BenchClient

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "recipes"))
from qualification_campaign import identity                                       # noqa: E402
from qualification_campaign.session import SessionError, validate_session        # noqa: E402

RECIPE = "recipes/qualification_campaign/test_m40_identity.py"
TOKEN = "pytest-client-test-token"
SELECT_ID = "test_m40_id_hardware_identity"
SELECT_PHYS = "test_m40_phys_b_cable_association"
KEY = bytes(range(32))
MAC_A, MAC_B = "aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"


def result(mac=MAC_A, chip="esp32s3", size=16777216, flash_id=0x164068):
    return {"chip": chip, "mac": mac, "flash_size": size, "flash_id": flash_id}


def private_dir(tmp_path, name="private"):
    path = tmp_path / name
    path.mkdir(mode=0o700)
    return path


# --- canonical form and fingerprints ---------------------------------------------------------------------------------------------------------
def test_the_canonical_form_is_versioned_and_exact():
    assert identity.canonical("esp32s3", "AA:BB:CC:DD:EE:01", 4194304, 0x164068) == (
        "iobewi-identity-v1|chip=esp32s3|mac=aabbccddee01|flash_size=4194304|flash_id=0x164068")
    assert identity.CANONICAL_VERSION == "iobewi-identity-v1"


@pytest.mark.parametrize("mac", ["aa:bb:cc:dd:ee:01", "AA-BB-CC-DD-EE-01", "aabbccddee01", " AA:bb:CC:dd:EE:01 "])
def test_every_common_mac_spelling_normalises_to_the_same_form(mac):
    assert identity.normalize_mac(mac) == "aabbccddee01"


@pytest.mark.parametrize("bad", [None, 12, "", "zz:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee", "aa:bb:cc:dd:ee:01:02", "aabbccddee0g"])
def test_a_malformed_mac_is_refused_without_echoing_it(bad):
    with pytest.raises(identity.IdentityError) as error:
        identity.normalize_mac(bad)
    assert not bad or str(bad) not in str(error.value)


@pytest.mark.parametrize("change", [{"chip": ""}, {"chip": "ESP32"}, {"chip": 9}, {"size": 0}, {"size": "16"}, {"size": True}, {"flash_id": -1}, {"flash_id": "0x1"}])
def test_malformed_chip_size_or_flash_id_is_refused(change):
    with pytest.raises(identity.IdentityError):
        identity.fingerprint(result(**change), KEY)


def test_the_digest_is_hmac_sha256_of_the_canonical_form():
    fp = identity.fingerprint(result(), KEY)
    expected = hmac.new(KEY, identity.canonical("esp32s3", MAC_A, 16777216, 0x164068).encode(), hashlib.sha256).hexdigest()
    assert fp["digest"] == expected and fp["version"] == "iobewi-identity-v1" and len(fp["digest"]) == 64
    assert fp["fields"] == {"chip": "esp32s3", "flash_size": 16777216, "flash_id": "0x164068",
                            "mac": hmac.new(KEY, b"mac|aabbccddee01", hashlib.sha256).hexdigest()}
    assert fp["mac_suffix"] == "ee01"


def test_the_digest_depends_on_the_key_and_on_every_field():
    base = identity.fingerprint(result(), KEY)
    assert identity.fingerprint(result(), bytes(reversed(KEY)))["digest"] != base["digest"]
    for change, field in (({"mac": MAC_B}, "mac"), ({"flash_id": 0x174068}, "flash_id"), ({"size": 4194304}, "flash_size"), ({"chip": "esp32c3"}, "chip")):
        other = identity.fingerprint(result(**change), KEY)
        assert other["digest"] != base["digest"] and identity.different_fields(base, other) == [field], field


def test_a_fingerprint_never_contains_the_raw_mac_or_the_key():
    text = json.dumps(identity.fingerprint(result(), KEY))
    for forbidden in ("aabbccddee01", "AA:BB", "aa:bb:cc", MAC_A, KEY.hex(), "ddee01"):
        assert forbidden not in text, forbidden
    assert "ee01" in text, "only the last two octets are allowed for diagnostics"


def test_a_result_missing_a_field_is_refused_and_the_key_must_be_32_bytes():
    with pytest.raises(identity.IdentityError):
        identity.fingerprint({"chip": "esp32s3", "mac": MAC_A, "flash_size": 1}, KEY)
    with pytest.raises(identity.IdentityError):
        identity.fingerprint(result(), b"short")
    with pytest.raises(identity.IdentityError):
        identity.fingerprint("not a dict", KEY)


def test_scrub_empties_the_object_that_carried_the_identity():
    raw = result()
    same = raw
    identity.scrub(raw)
    assert same == {} and raw == {}
    identity.scrub(None)                                                      # harmless on anything else


def test_stability_needs_two_reads_and_names_the_changed_field():
    a1, a2 = identity.fingerprint(result(), KEY), identity.fingerprint(result(), KEY)
    assert identity.check_stable([a1, a2], "A") == []
    assert identity.check_stable([a1], "A") and identity.check_stable([], "A")
    for change, field in (({"mac": MAC_B}, "mac"), ({"flash_id": 1}, "flash_id"), ({"size": 4194304}, "flash_size"), ({"chip": "esp32c3"}, "chip")):
        problems = identity.check_stable([a1, identity.fingerprint(result(**change), KEY)], "A")
        assert problems and field in problems[0]


def test_distinct_cards_need_different_digests_and_macs_but_may_share_a_flash_id():
    a, b = identity.fingerprint(result(MAC_A), KEY), identity.fingerprint(result(MAC_B), KEY)
    assert identity.check_distinct(a, b, ("A", "B")) == [], "an equal flash_id of two units of one model is normal"
    assert len(identity.check_distinct(a, identity.fingerprint(result(MAC_A), KEY), ("A", "B"))) == 2
    other_model = identity.fingerprint(result(MAC_A, size=4194304), KEY)
    assert identity.check_distinct(a, other_model, ("A", "B")) == [f"A and B report the same MAC"], "same MAC under another model is still the same MAC"


# --- the key ---------------------------------------------------------------------------------------------------------------------------------------
def test_the_key_is_created_once_private_random_and_never_replaced(tmp_path):
    directory = private_dir(tmp_path)
    path = directory / "identity.key"
    old = os.umask(0)
    try:
        identity.create_key(path)
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_size == 32 and len(identity.load_key(path)) == 32
    first = path.read_bytes()
    with pytest.raises(FileExistsError):
        identity.create_key(path)
    assert path.read_bytes() == first, "an existing key is never replaced"
    other = directory / "other.key"
    identity.create_key(other)
    assert other.read_bytes() != first, "keys come from a CSPRNG"


def test_a_key_is_refused_in_a_shared_directory_or_through_a_link(tmp_path):
    open_directory = tmp_path / "open"
    open_directory.mkdir(mode=0o755)
    os.chmod(open_directory, 0o755)
    with pytest.raises(identity.IdentityError):
        identity.create_key(open_directory / "identity.key")
    directory = private_dir(tmp_path)
    target = directory / "target"
    target.write_bytes(b"x" * 32)
    os.chmod(target, 0o600)
    (directory / "link.key").symlink_to(target)
    with pytest.raises(identity.IdentityError):
        identity.load_key(directory / "link.key")
    with pytest.raises(OSError):
        identity.create_key(directory / "link.key")
    assert target.read_bytes() == b"x" * 32


@pytest.mark.parametrize("content,mode", [(b"x" * 32, 0o644), (b"x" * 32, 0o640), (b"x" * 31, 0o600), (b"x" * 33, 0o600), (b"", 0o600)])
def test_a_key_with_wrong_permissions_or_size_is_refused(tmp_path, content, mode):
    path = private_dir(tmp_path) / "identity.key"
    path.write_bytes(content)
    os.chmod(path, mode)
    with pytest.raises(identity.IdentityError):
        identity.load_key(path)


# --- the reference ---------------------------------------------------------------------------------------------------------------------------------
def fingerprints():
    return {"A": identity.fingerprint(result(MAC_A), KEY), "B": identity.fingerprint(result(MAC_B, size=4194304, flash_id=0x164068), KEY)}


def test_the_reference_holds_digests_and_suffixes_never_a_mac():
    document = identity.reference_document(fingerprints(), created="2026-11-11T00:00:00Z")
    text = json.dumps(document)
    for forbidden in ("aabbccddee01", "aabbccddee02", MAC_A, MAC_B, KEY.hex()):
        assert forbidden not in text
    assert set(document["cards"]) == {"A", "B"} and document["cards"]["A"]["mac_suffix"] == "ee01"
    assert identity.validate_reference(document, ["A", "B"]) is document


@pytest.mark.parametrize("damage", [
    lambda d: d["cards"].pop("B"), lambda d: d.__setitem__("version", 2), lambda d: d.__setitem__("canonical", "iobewi-identity-v2"),
    lambda d: d["cards"]["A"]["fields"].pop("mac"), lambda d: d["cards"]["A"].__setitem__("digest", "short"),
    lambda d: d["cards"]["A"].__setitem__("mac_suffix", "zzzz"), lambda d: d["cards"].__setitem__("C", d["cards"]["A"]),
    lambda d: d["cards"]["A"]["fields"].__setitem__("mac", "x" * 64), lambda d: d.__setitem__("cards", [])])
def test_a_partial_or_malformed_reference_is_never_valid(damage):
    document = json.loads(json.dumps(identity.reference_document(fingerprints())))
    damage(document)
    with pytest.raises(identity.IdentityError):
        identity.validate_reference(document, ["A", "B"])


def test_the_reference_is_written_once_atomically_private_and_never_overwritten(tmp_path):
    directory = private_dir(tmp_path)
    path = directory / "reference.json"
    document = identity.reference_document(fingerprints())
    old = os.umask(0)
    try:
        identity.write_reference(path, document)
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and identity.load_reference(path, ["A", "B"]) == document
    before = path.read_bytes()
    other = identity.reference_document({"A": identity.fingerprint(result(MAC_B), KEY), "B": identity.fingerprint(result(MAC_A), KEY)})
    with pytest.raises(identity.ReferenceExists):
        identity.write_reference(path, other)
    assert path.read_bytes() == before, "an existing reference is never overwritten"
    assert [p.name for p in directory.iterdir()] == ["reference.json"], "no temporary file is left behind"
    text = path.read_text()
    assert "aabbccddee0" not in text


def test_an_interrupted_enrolment_leaves_no_reference_and_no_temporary_file(tmp_path, monkeypatch):
    directory = private_dir(tmp_path)
    path = directory / "reference.json"
    document = identity.reference_document(fingerprints())
    real_link = os.link
    monkeypatch.setattr(identity.os, "link", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("crash before the final name appears")))
    with pytest.raises(OSError):
        identity.write_reference(path, document)
    monkeypatch.setattr(identity.os, "link", real_link)
    assert list(directory.iterdir()) == [], "a crash before the link leaves nothing: the final name only ever appears complete"
    real_write = os.write
    calls = []

    def short_then_crash(fd, data):
        calls.append(1)
        if len(calls) > 1:
            raise OSError("disk full")
        return real_write(fd, data[: max(1, len(data) // 2)])
    monkeypatch.setattr(identity.os, "write", short_then_crash)
    with pytest.raises(OSError):
        identity.write_reference(path, document)
    assert list(directory.iterdir()) == [], "a half-written temporary file never becomes the reference"


def test_a_reference_in_a_shared_directory_or_behind_a_link_is_refused(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o755)
    with pytest.raises(identity.IdentityError):
        identity.write_reference(shared / "reference.json", identity.reference_document(fingerprints()))
    directory = private_dir(tmp_path)
    victim = directory / "victim.json"
    victim.write_text("keep")
    os.chmod(victim, 0o600)
    (directory / "reference.json").symlink_to(victim)
    with pytest.raises(identity.ReferenceExists):
        identity.write_reference(directory / "reference.json", identity.reference_document(fingerprints()))
    assert victim.read_text() == "keep"
    with pytest.raises(identity.IdentityError):
        identity.load_reference(directory / "reference.json", ["A", "B"])


def test_a_reference_with_open_permissions_or_invalid_content_is_refused(tmp_path):
    directory = private_dir(tmp_path)
    path = directory / "reference.json"
    path.write_text(json.dumps(identity.reference_document(fingerprints())))
    os.chmod(path, 0o644)
    with pytest.raises(identity.IdentityError):
        identity.load_reference(path, ["A", "B"])
    os.chmod(path, 0o600)
    path.write_text("{not json")
    with pytest.raises(identity.IdentityError):
        identity.load_reference(path, ["A", "B"])


def test_comparing_with_the_reference_names_the_differing_fields_and_never_replaces_it():
    reference = identity.reference_document(fingerprints())
    assert identity.compare_reference(reference, fingerprints()) == []
    changed = {"A": identity.fingerprint(result(MAC_B), KEY), "B": fingerprints()["B"]}
    problems = identity.compare_reference(reference, changed)
    assert len(problems) == 1 and "mac" in problems[0] and "A" in problems[0]
    assert identity.compare_reference(reference, {"C": fingerprints()["A"]}) == ["C: no entry in the reference"]
    flash = {"A": fingerprints()["A"], "B": identity.fingerprint(result(MAC_B, size=4194304, flash_id=0x174068), KEY)}
    assert "flash_id" in identity.compare_reference(reference, flash)[0]
    assert reference == identity.reference_document(fingerprints(), created=reference["created"]), "the reference object was not modified"


# --- session ---------------------------------------------------------------------------------------------------------------------------------------
def base_session(tmp_path, consents=("M40-ID",), **change):
    private = tmp_path / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    document = {"version": 1, "board_id": "board", "chip": "esp32s3", "flash_size": 4194304, "transport": "external_uart",
                "partition_table_offset": None, "evidence_root": str(tmp_path / "evidence"), "accept_unknown_transport": True,
                "operator_timeout_s": 30, "consent": {"scenarios": list(consents)}, "second_board": {"board_id": "board2"},
                "m40": {"sample_interval_s": 0.1,
                        "identity": {"key_file": str(private / "identity.key"), "reference_file": str(private / "reference.json"), "create_key": True},
                        "phys_b": {"disconnect_timeout_s": 5, "return_timeout_s": 8, "settle_s": 1}}}
    return {**document, **change}


def test_the_new_consents_and_sections_validate(tmp_path):
    cfg = validate_session(base_session(tmp_path, consents=("M40-ID", "M40-ID-ENROLL", "M40-PHYS-B", "M40-PHYS-B-ID")))
    assert cfg["m40"]["identity"]["create_key"] is True and cfg["m40"]["phys_b"] == {"disconnect_timeout_s": 5.0, "return_timeout_s": 8.0, "settle_s": 1.0}
    example = validate_session(json.loads((BENCH / "recipes/qualification_campaign/session.m40.example.json").read_text()))
    assert example["consent"]["scenarios"] == [] and example["m40"]["identity"]["create_key"] is False, "the template creates nothing and consents to nothing"


@pytest.mark.parametrize("change", [
    {"key_file": "relative.key"}, {"reference_file": "relative.json"}, {"key_file": ""}, {"key_file": 3}, {"key_file": "/p/dir/"},
    {"reference_file": "SAME"}, {"create_key": "yes"}, {"create_key": 1}, {"extra": 1},
    {"key_file": "EVIDENCE/identity.key"}, {"reference_file": "EVIDENCE/sub/reference.json"}])
def test_invalid_identity_sections_are_refused(tmp_path, change):
    document = base_session(tmp_path)
    section = document["m40"]["identity"]
    for key, value in change.items():
        section[key] = section["key_file"] if value == "SAME" else str(tmp_path / "evidence" / value.split("/", 1)[1]) if str(value).startswith("EVIDENCE") else value
    with pytest.raises(SessionError):
        validate_session(document)


@pytest.mark.parametrize("section", [{}, {"key_file": "/a/k"}, {"reference_file": "/a/r"}])
def test_an_identity_section_needs_both_files(tmp_path, section):
    document = base_session(tmp_path)
    document["m40"]["identity"] = section
    with pytest.raises(SessionError):
        validate_session(document)


@pytest.mark.parametrize("section", [
    {}, {"disconnect_timeout_s": 5, "return_timeout_s": 5}, {"disconnect_timeout_s": 4, "return_timeout_s": 5, "settle_s": 0},
    {"disconnect_timeout_s": 5, "return_timeout_s": 901, "settle_s": 0}, {"disconnect_timeout_s": 5, "return_timeout_s": 5, "settle_s": -1},
    {"disconnect_timeout_s": True, "return_timeout_s": 5, "settle_s": 0}, {"disconnect_timeout_s": 5, "return_timeout_s": 5, "settle_s": 0, "x": 1}])
def test_invalid_phys_b_sections_are_refused(tmp_path, section):
    document = base_session(tmp_path)
    document["m40"]["phys_b"] = section
    with pytest.raises(SessionError):
        validate_session(document)


# --- simulated bench -------------------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def sim():
    started = []

    def start(env=None, control=False):
        command = [sys.executable, str(BENCH / "python/tests/sim_server.py"), "--second", "--chip", "esp32s3"] + (["--control"] if control else [])
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE if control else None, text=True,
                                   env={**os.environ, **(env or {})})
        started.append(process)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(10)
        url = json.loads(process.stdout.readline())["url"]
        client = BenchClient(url, TOKEN)
        deadline = time.monotonic() + 10
        while len(client.devices()) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        return url, client, process
    yield start
    for process in started:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


ENV_ID = {"SIM_IDENTITY": "{}", "SIM_IDENTIFY_EVENTS": "1"}


def sim_fingerprint(device, mac=None, size=4194304, flash_id=0x164068, key=KEY):
    """The fingerprint of the simulated card `device` under KEY (mirrors sim_agent's default identity)."""
    default = "de:ad:be:ef:00:" + format(hashlib.sha256(device.encode()).digest()[0], "02x")
    return identity.fingerprint(result(mac or default, size=size, flash_id=flash_id), key)


def run_recipe(tmp_path, url, selector, document, *, extra=(), operator=None, timeout=240):
    """Run one recipe in a subprocess. `operator(line)` is called for every stdout line (an operator prompt starts with 'OPERATOR>>')."""
    path = tmp_path / "session.json"
    path.write_text(json.dumps(document))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": TOKEN, "BENCH_URL": url, "BENCH_QUAL_SESSION": str(path)}
    junit = tmp_path / "junit.xml"
    process = subprocess.Popen([sys.executable, "-m", "pytest", f"{RECIPE}::{selector}", "--bench", "--qual-allow-dirty", "--bench-results", str(tmp_path / "runs"),
                                f"--junitxml={junit}", "-q", "-rs", "-p", "no:cacheprovider", "--tb=short", *extra],
                               cwd=BENCH, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    watchdog = threading.Timer(timeout, process.kill)
    watchdog.start()
    try:
        for line in process.stdout:
            lines.append(line)
            if operator and line.startswith("OPERATOR>>"):
                operator(line)
        process.wait()
    finally:
        watchdog.cancel()
    reports = {i["id"]: i for f in (tmp_path / "evidence").glob("*/report.json") for i in json.loads(f.read_text())["tests"]}
    return process.returncode, "".join(lines), reports, junit


def posts(tmp_path):
    rows = [json.loads(line) for path in (tmp_path / "evidence").glob("*/requests.jsonl") for line in path.read_text().splitlines()]
    return [r for r in rows if r["method"] == "POST" and r["path"] in ("/api/v1/operations", "/api/v1/leases")]


def operations(tmp_path):
    return [r for r in posts(tmp_path) if r["path"] == "/api/v1/operations"]


def deletes(tmp_path):
    rows = [json.loads(line) for path in (tmp_path / "evidence").glob("*/requests.jsonl") for line in path.read_text().splitlines()]
    return [r for r in rows if r["method"] == "DELETE" and r["path"].startswith("/api/v1/leases/")]


def campaign(tmp_path, prefix):
    return next((tmp_path / "evidence").glob(f"{prefix}-*"))


def assert_no_raw_identity(tmp_path, *extra_texts):
    """No raw MAC (in any spelling), no key byte string and no hex of the key in anything a run left behind: evidence, runs, junit, stdout."""
    needles = ("de:ad:be:ef", "deadbeef", "DE:AD:BE", "DEADBEEF", "de-ad-be", "aabbccddee", KEY.hex())
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name not in ("identity.key",) and "bench" not in path.relative_to(tmp_path).parts[:1]:      # (a copied tree holds the simulator's source)
            data = path.read_bytes()
            text = data.decode("utf-8", "replace")
            for needle in needles:
                assert needle not in text, f"{needle!r} found in {path.relative_to(tmp_path)}"
    for text in extra_texts:
        for needle in needles:
            assert needle not in text, f"{needle!r} found in the captured output"


def install_key(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    (private / "identity.key").write_bytes(KEY)
    os.chmod(private / "identity.key", 0o600)
    return private / "identity.key"


def sim_reference(tmp_path, **overrides):
    """A reference recorded for the two simulated cards (as a previous M40-ID would have), overridable per card."""
    install_key(tmp_path)
    cards = {"board": sim_fingerprint("board"), "board2": sim_fingerprint("board2")}
    cards.update({name: fp for name, fp in overrides.items()})
    path = tmp_path / "private" / "reference.json"
    identity.write_reference(path, identity.reference_document(cards))
    return path


# --- M40-ID end to end -------------------------------------------------------------------------------------------------------------------------
def run_id(tmp_path, url, document=None, **kwargs):
    return run_recipe(tmp_path, url, SELECT_ID, document or base_session(tmp_path), **kwargs)


def test_the_two_simulated_cards_have_different_default_identities():
    assert sim_fingerprint("board")["digest"] != sim_fingerprint("board2")["digest"]


def test_m40_id_passes_with_two_identify_per_card_and_leaves_no_raw_identity(sim, tmp_path):
    url, client, _ = sim(ENV_ID)
    code, output, reports, junit = run_id(tmp_path, url)
    assert code == 0 and reports["M40-ID"]["status"] == "PASS", output[-2000:]
    folder = campaign(tmp_path, "M40-ID")
    names = lambda board: [json.loads(line)["command"] for line in next(folder.glob(f"{board}/commands.jsonl")).read_text().splitlines()]   # noqa: E731
    assert names("board-0") == ["identify", "identify"] and names("board-second") == ["identify", "identify"]
    assert len(operations(tmp_path)) == 4, "exactly 4 identify: no replay"
    document = json.loads((folder / "m40-id-fingerprints.json").read_text())
    assert document["phase"] == "checks" and document["error"] is None and document["canonical"] == "iobewi-identity-v1"
    for name in ("board", "board2"):
        reads = document["reads"][name]
        assert len(reads) == 2 and reads[0]["digest"] == reads[1]["digest"] and all(r["loader"] and r["console"] for r in reads)
        generated = (tmp_path / "private" / "identity.key").read_bytes()
        assert reads[0]["digest"] == sim_fingerprint(name, key=generated)["digest"], "the digest is the HMAC of the canonical form under the private key"
    assert document["reads"]["board"][0]["digest"] != document["reads"]["board2"][0]["digest"]
    steps = [json.loads(line)["step"] for line in (folder / "m40-id-steps.jsonl").read_text().splitlines()]
    assert steps == ["initial", "both reserved", "identify 1 board", "identify 1 board2", "identify 2 board", "identify 2 board2"]
    for name in ("m40-id-fingerprints.json", "m40-id-steps.jsonl"):
        assert stat.S_IMODE((folder / name).stat().st_mode) == 0o600, name
    text = " ".join(reports["M40-ID"]["evidence"])
    assert "reference: no reference exists and M40-ID-ENROLL was not granted" in text and "NOT shown" in text
    assert not (tmp_path / "private" / "reference.json").exists(), "nothing is recorded without the enrolment consent"
    assert stat.S_IMODE((tmp_path / "private" / "identity.key").stat().st_mode) == 0o600 and (tmp_path / "private" / "identity.key").stat().st_size == 32
    assert all(client.device(n)["reservation"] is None for n in ("board", "board2"))
    assert_no_raw_identity(tmp_path, output)


def test_the_first_reference_is_recorded_once_after_a_qualifying_run_then_compared_never_replaced(sim, tmp_path):
    url, client, _ = sim(ENV_ID)
    document = base_session(tmp_path, consents=("M40-ID", "M40-ID-ENROLL"))
    code, output, reports, _ = run_id(tmp_path, url, document)
    assert reports["M40-ID"]["status"] == "PASS", output[-1500:]
    reference_path = tmp_path / "private" / "reference.json"
    assert stat.S_IMODE(reference_path.stat().st_mode) == 0o600 and "recorded ONCE" in " ".join(reports["M40-ID"]["evidence"])
    reference = identity.load_reference(reference_path, ["board", "board2"])
    key = (tmp_path / "private" / "identity.key").read_bytes()
    assert reference["cards"]["board"]["digest"] == identity.fingerprint(result("de:ad:be:ef:00:" + format(hashlib.sha256(b"board").digest()[0], "02x"), size=4194304), key)["digest"]
    assert [p.name for p in (tmp_path / "private").iterdir() if p.name.startswith(".")] == [], "no temporary file"
    before = reference_path.read_bytes()
    second = tmp_path / "second"
    second.mkdir()
    document2 = {**document, "evidence_root": str(second / "evidence")}
    code, output, reports, _ = run_recipe(second, url, SELECT_ID, document2)
    assert reports["M40-ID"]["status"] == "PASS" and "compared with the recorded reference" in " ".join(reports["M40-ID"]["evidence"])
    assert reference_path.read_bytes() == before, "an existing reference is compared, never re-enrolled, even with the enrolment consent"
    assert_no_raw_identity(tmp_path, output)


def test_a_different_identity_than_the_reference_is_a_fail_and_the_reference_stays(sim, tmp_path):
    url, client, _ = sim({**ENV_ID, "SIM_IDENTITY": json.dumps({"board2": {"mac": "de:ad:be:ef:00:77"}})})
    install_key(tmp_path)
    path = sim_reference(tmp_path)
    before = path.read_bytes()
    code, output, reports, _ = run_id(tmp_path, url, base_session(tmp_path, consents=("M40-ID", "M40-ID-ENROLL")))
    assert code != 0 and reports["M40-ID"]["status"] == "FAIL" and "board2" in " ".join(reports["M40-ID"]["incidents"] + [output])
    assert "mac" in output and path.read_bytes() == before, "a different identity never replaces the reference"
    assert_no_raw_identity(tmp_path, output)


@pytest.mark.parametrize("field,change", [("mac", {"mac": "de:ad:be:ef:00:99"}), ("flash_id", {"flash_id": 0x174068}), ("flash_size", {"flash_size": 8388608})])
def test_a_field_that_changes_between_the_two_reads_is_a_fail_naming_it(sim, tmp_path, field, change):
    url, client, _ = sim({**ENV_ID, "SIM_IDENTITY_CHANGE": json.dumps({"board2": {"after": 1, **change}})})
    code, output, reports, _ = run_id(tmp_path, url)
    assert code != 0 and reports["M40-ID"]["status"] == "FAIL", output[-1500:]
    assert f"differs from read 1 in ['{field}']" in output + " ".join(reports["M40-ID"]["incidents"])
    document = json.loads((campaign(tmp_path, "M40-ID") / "m40-id-fingerprints.json").read_text())
    assert len(document["reads"]["board2"]) == 2 and document["phase"] == "checks", "the four reads happened; the verdict is decided in the checks"
    assert len(operations(tmp_path)) == 4 and len(deletes(tmp_path)) == 2, "the four commands ran once and both reservations were released"
    assert_no_raw_identity(tmp_path, output)


def test_two_cards_with_the_same_identity_are_a_fail(sim, tmp_path):
    same = {"mac": "de:ad:be:ef:00:55", "flash_id": 0x164068, "flash_size": 4194304}
    url, client, _ = sim({**ENV_ID, "SIM_IDENTITY": json.dumps({"board": same, "board2": same})})
    code, output, reports, _ = run_id(tmp_path, url)
    assert code != 0 and reports["M40-ID"]["status"] == "FAIL" and "report the same hardware identity" in output + " ".join(reports["M40-ID"]["incidents"])
    assert "report the same MAC" in output
    assert_no_raw_identity(tmp_path, output)


def test_a_missing_console_return_is_indeterminate_keeps_the_reservation_and_never_replays(sim, tmp_path):
    url, client, _ = sim({**ENV_ID, "SIM_NO_CONSOLE_RETURN": "board2"})
    code, output, reports, _ = run_id(tmp_path, url)
    assert code != 0 and reports["M40-ID"]["status"] == "INDETERMINATE", (reports, output[-1500:])
    assert "no `console opened` event" in output + " ".join(reports["M40-ID"]["incidents"])
    assert len(operations(tmp_path)) == 2, "the identify of the card without a proven console return is never repeated, nothing else ran"
    assert client.device("board2")["reservation"] is not None, "a card whose state is unknown stays reserved"
    assert not [d for d in deletes(tmp_path) if "second" in d.get("client", "")], "no forced release of the uncertain reservation"
    document = json.loads((campaign(tmp_path, "M40-ID") / "m40-id-fingerprints.json").read_text())
    assert document["error"] == "PendingOperation" and len(document["reads"]["board2"]) == 1 and document["reads"]["board2"][0]["console"] is False
    assert_no_raw_identity(tmp_path, output)


def test_a_generation_change_during_the_run_is_indeterminate(sim, tmp_path):
    url, client, _ = sim({**ENV_ID, "SIM_REENUMERATE_AFTER_IDENTIFY": "board2:1"})
    code, output, reports, _ = run_id(tmp_path, url)
    assert code != 0 and reports["M40-ID"]["status"] == "INDETERMINATE", (reports, output[-1500:])
    assert len(operations(tmp_path)) <= 3, "no identify is replayed after the card re-registered"
    assert_no_raw_identity(tmp_path, output)


@pytest.mark.parametrize("helper,verdict,fragment", [
    ("check_event_kinds = _injected\n", "FAIL", "injected defect"), ("check_commands = _injected\n", "FAIL", "replay or foreign command"),
    ("check_contiguous = _injected\n", "FAIL", "injected defect"),
    ("def drain_events(*args, **kwargs):\n    from iobewi_bench.client import ContinuityError\n    raise ContinuityError('server epoch changed (injected)')\n", "INDETERMINATE", "epoch changed")])
def test_the_wiring_of_the_foreign_event_replay_and_epoch_checks(sim, tmp_path, helper, verdict, fragment):
    url, client, _ = sim(ENV_ID)
    code, output, reports, _ = run_with_defect(tmp_path, url, SELECT_ID, base_session(tmp_path), "\n_injected = lambda *a, **k: ['injected defect']\n" + helper)
    assert code != 0 and reports["M40-ID"]["status"] == verdict, (helper[:30], output[-900:])
    assert fragment in output + " ".join(reports["M40-ID"]["incidents"])


def run_with_defect(tmp_path, url, selector, document, defect, *, extra=(), operator=None):
    """Run the recipe from a COPY of the bench tree whose m40.py got `defect` appended (the simulated cards stay healthy)."""
    import shutil
    copy = tmp_path / "bench"
    shutil.copytree(BENCH, copy, ignore=shutil.ignore_patterns("node_modules", "__pycache__", ".bench-runs", ".pytest_cache"))
    with (copy / "recipes/qualification_campaign/m40.py").open("a") as stream:
        stream.write(defect)
    return run_in(copy, tmp_path, url, selector, document, extra=extra, operator=operator)


def run_with_identity_defect(tmp_path, url, selector, document, defect, *, extra=()):
    import shutil
    copy = tmp_path / "bench"
    shutil.copytree(BENCH, copy, ignore=shutil.ignore_patterns("node_modules", "__pycache__", ".bench-runs", ".pytest_cache"))
    with (copy / "recipes/qualification_campaign/identity.py").open("a") as stream:
        stream.write(defect)
    return run_in(copy, tmp_path, url, selector, document, extra=extra)


def run_in(copy, tmp_path, url, selector, document, *, extra=(), operator=None):
    path = tmp_path / "session.json"
    path.write_text(json.dumps(document))
    env = {**os.environ, "BENCH_CLIENT_TOKEN": TOKEN, "BENCH_URL": url, "BENCH_QUAL_SESSION": str(path)}
    process = subprocess.Popen([sys.executable, "-m", "pytest", f"{RECIPE}::{selector}", "--bench", "--qual-allow-dirty", "--bench-results", str(tmp_path / "runs"),
                                "-q", "-rs", "-p", "no:cacheprovider", "--tb=short", *extra], cwd=copy, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines, watchdog = [], threading.Timer(240, process.kill)
    watchdog.start()
    try:
        for line in process.stdout:
            lines.append(line)
            if operator and line.startswith("OPERATOR>>"):
                operator(line)
        process.wait()
    finally:
        watchdog.cancel()
    reports = {i["id"]: i for f in (tmp_path / "evidence").glob("*/report.json") for i in json.loads(f.read_text())["tests"]}
    return process.returncode, "".join(lines), reports, None


def test_a_missing_key_blocks_unless_its_creation_was_explicitly_enabled(sim, tmp_path):
    url, client, _ = sim(ENV_ID)
    document = base_session(tmp_path)
    document["m40"]["identity"]["create_key"] = False
    code, output, reports, _ = run_id(tmp_path, url, document)
    assert reports["M40-ID"]["status"] == "BLOCKED" and "create_key" in output + " ".join(reports["M40-ID"]["incidents"])
    assert not (tmp_path / "private" / "identity.key").exists() and not operations(tmp_path) and not posts(tmp_path)


@pytest.mark.parametrize("damage", ["partial", "garbage", "open-permissions", "unknown-card"])
def test_an_unusable_reference_blocks_and_is_never_repaired_or_replaced(sim, tmp_path, damage):
    url, client, _ = sim(ENV_ID)
    install_key(tmp_path)
    path = sim_reference(tmp_path)
    if damage == "partial":
        document = json.loads(path.read_text())
        document["cards"].pop("board2")
        path.write_text(json.dumps(document))
    elif damage == "garbage":
        path.write_text("{truncated")
    elif damage == "open-permissions":
        os.chmod(path, 0o644)
    else:
        document = json.loads(path.read_text())
        document["cards"]["intruder"] = document["cards"]["board"]
        path.write_text(json.dumps(document))
    before = path.read_bytes()
    code, output, reports, _ = run_id(tmp_path, url, base_session(tmp_path, consents=("M40-ID", "M40-ID-ENROLL")))
    assert reports["M40-ID"]["status"] == "BLOCKED" and "never repaired or replaced" in output + " ".join(reports["M40-ID"]["incidents"])
    assert path.read_bytes() == before and not posts(tmp_path), "no command, no repair, no replacement"


def test_m40_id_is_blocked_before_any_command(sim, tmp_path):
    url, client, _ = sim(ENV_ID)
    cases = {"consent": base_session(tmp_path, consents=()), "section": {**base_session(tmp_path), "m40": {"sample_interval_s": 0.1}}}
    for name, document in cases.items():
        work = tmp_path / name
        work.mkdir()
        document = {**document, "evidence_root": str(work / "evidence")}
        code, output, reports, _ = run_recipe(work, url, SELECT_ID, document)
        assert "BLOCKED" in output and not reports and not posts(work), name
    work = tmp_path / "locals"
    work.mkdir()
    code, output, reports, _ = run_recipe(work, url, SELECT_ID, {**base_session(tmp_path), "evidence_root": str(work / "evidence")}, extra=("--showlocals",))
    assert "BLOCKED" in output and "--showlocals" in output and not posts(work), "--showlocals would print raw identities"
    held = client.reserve("board2", ttl=120)
    try:
        work = tmp_path / "held"
        work.mkdir()
        code, output, reports, _ = run_recipe(work, url, SELECT_ID, {**base_session(tmp_path), "evidence_root": str(work / "evidence")})
    finally:
        held.close()
    assert reports["M40-ID"]["status"] == "BLOCKED" and "reserved" in " ".join(reports["M40-ID"]["incidents"])


def write_fails(*names):
    return ("\nimport iobewi_bench.qualification as _q\n_real_write = _q.Campaign.write\n_names = " + repr(list(names)) +
            "\n\n\ndef _write(self, filename, value):\n    if filename in _names:\n        raise OSError('disk full (injected)')\n    return _real_write(self, filename, value)\n\n\n"
            "_q.Campaign.write = _write\n")


def test_an_evidence_failure_never_replaces_the_error_in_flight_and_the_other_files_survive(sim, tmp_path):
    url, client, _ = sim({**ENV_ID, "SIM_IDENTITY_CHANGE": json.dumps({"board2": {"after": 1, "mac": "de:ad:be:ef:00:99"}})})
    code, output, reports, _ = run_with_defect(tmp_path, url, SELECT_ID, base_session(tmp_path), write_fails("m40-id-fingerprints.json"))
    report = reports["M40-ID"]
    assert code != 0 and report["status"] == "FAIL" and "m40-id-fingerprints.json not written (OSError)" in " ".join(report["incidents"])
    assert "differs from read 1" in output + " ".join(report["incidents"]), "the ORIGINAL error stays the verdict"
    folder = campaign(tmp_path, "M40-ID")
    assert (folder / "m40-id-steps.jsonl").is_file() and not (folder / "m40-id-fingerprints.json").exists(), "the other evidence file is still written"
    assert_no_raw_identity(tmp_path, output)


def test_an_unwritten_evidence_file_on_an_otherwise_clean_run_is_never_a_pass(sim, tmp_path):
    url, client, _ = sim(ENV_ID)
    code, output, reports, _ = run_with_defect(tmp_path, url, SELECT_ID, base_session(tmp_path), write_fails("m40-id-fingerprints.json"))
    assert reports["M40-ID"]["status"] == "FAIL" and "could not be written" in output + " ".join(reports["M40-ID"]["incidents"])
    assert (campaign(tmp_path, "M40-ID") / "m40-id-steps.jsonl").is_file()


def test_an_interrupted_enrolment_records_no_reference_and_leaves_nothing_behind(sim, tmp_path):
    url, client, _ = sim(ENV_ID)
    defect = "\n_real_link = os.link\n\n\ndef _crash(*args, **kwargs):\n    raise OSError('crash before the final name appears (injected)')\n\n\nos.link = _crash\n"
    code, output, reports, _ = run_with_identity_defect(tmp_path, url, SELECT_ID, base_session(tmp_path, consents=("M40-ID", "M40-ID-ENROLL")), defect)
    assert code != 0 and reports["M40-ID"]["status"] == "FAIL" and "no valid reference exists" in output + " ".join(reports["M40-ID"]["incidents"])
    assert sorted(p.name for p in (tmp_path / "private").iterdir()) == ["identity.key"], "no reference, no temporary file"
    assert len(operations(tmp_path)) == 4, "the identification itself was complete"


def test_a_reference_that_appears_during_the_run_is_never_overwritten(sim, tmp_path):
    url, client, _ = sim(ENV_ID)
    planted = tmp_path / "private" / "reference.json"
    defect = (f"\n_real_write = write_reference\n\n\ndef write_reference(path, document):\n    import os as _os\n    fd = _os.open({str(planted)!r}, _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL, 0o600)\n"
              "    _os.write(fd, b'planted by another process')\n    _os.close(fd)\n    return _real_write(path, document)\n")
    code, output, reports, _ = run_with_identity_defect(tmp_path, url, SELECT_ID, base_session(tmp_path, consents=("M40-ID", "M40-ID-ENROLL")), defect)
    assert reports["M40-ID"]["status"] == "FAIL" and "never overwritten" in output + " ".join(reports["M40-ID"]["incidents"])
    assert planted.read_bytes() == b"planted by another process"


# --- M40-PHYS-B end to end ---------------------------------------------------------------------------------------------------------------------
def sim_command(process, device, down_s):
    process.stdin.write(json.dumps({"device": device, "cmd": "unplug", "down_s": down_s}) + "\n")
    process.stdin.flush()


def operator(process, device="board2", down_s=2.0, *, then=None):
    """The simulated operator: reacts to the recipe's prompt by unplugging `device` for `down_s` seconds (and optionally a second gesture later)."""
    def act(line):
        sim_command(process, device, down_s)
        if then:
            threading.Timer(then[0], sim_command, args=(process, then[1], then[2])).start()
    return act


def phys_session(tmp_path, consents=("M40-PHYS-B",), **change):
    document = base_session(tmp_path, consents=consents)
    document["m40"]["phys_b"] = {"disconnect_timeout_s": 5, "return_timeout_s": 8, "settle_s": 1, **change}
    return document


def run_phys(tmp_path, url, process, document, *, act, extra=()):
    return run_recipe(tmp_path, url, SELECT_PHYS, document, extra=extra, operator=act)


def test_phys_b_proves_the_cable_association_while_a_stays_untouched(sim, tmp_path):
    url, client, process = sim({}, control=True)
    code, output, reports, junit = run_phys(tmp_path, url, process, phys_session(tmp_path), act=operator(process))
    assert code == 0 and reports["M40-PHYS-B"]["status"] == "PASS", output[-2500:]
    folder = campaign(tmp_path, "M40-PHYS-B")
    text = " ".join(reports["M40-PHYS-B"]["evidence"])
    assert "PHYSICAL ASSOCIATION" in text and "NEW generation" in text and "hardware identity NOT checked" in text
    timeline = json.loads((folder / "m40-phys-b-timeline.json").read_text())
    kinds = [e["kind"] for e in timeline["events_of_the_replugged_card"]]
    assert kinds.index("disconnected") < kinds.index("connected") and timeline["phase"] == "checks" and timeline["error"] is None
    assert not posts(tmp_path), "observation only: no reservation, no command"
    steps = [json.loads(line)["step"] for line in (folder / "m40-phys-b-steps.jsonl").read_text().splitlines()]
    assert steps == ["initial", "board2 lost", "board2 returned"]
    observed = [json.loads(line) for line in (folder / "m40-observed-card.jsonl").read_text().splitlines()]
    assert observed[-1]["terminated"] and observed[-1]["final_sampled"] and len([r for r in observed if r["type"] == "sample"]) >= 3
    assert stat.S_IMODE((folder / "m40-phys-b-timeline.json").stat().st_mode) == 0o600
    assert client.device("board2")["connected"] and client.device("board2")["reservation"] is None


def test_the_wrong_cable_is_a_fail(sim, tmp_path):
    url, client, process = sim({}, control=True)
    code, output, reports, _ = run_phys(tmp_path, url, process, phys_session(tmp_path), act=operator(process, device="board", down_s=2.0))
    assert code != 0 and reports["M40-PHYS-B"]["status"] == "FAIL", output[-1500:]
    assert "wrong cable" in output + " ".join(reports["M40-PHYS-B"]["incidents"])
    timeline = json.loads((campaign(tmp_path, "M40-PHYS-B") / "m40-phys-b-timeline.json").read_text())
    assert timeline["phase"] == "gesture" and timeline["error"] == "AssertionError", "the evidence of the wrong gesture is kept"
    assert not posts(tmp_path)


def test_a_card_that_never_comes_back_is_indeterminate_and_nothing_is_replayed(sim, tmp_path):
    url, client, process = sim({}, control=True)
    code, output, reports, _ = run_phys(tmp_path, url, process, phys_session(tmp_path), act=operator(process, down_s=3600))
    assert code != 0 and reports["M40-PHYS-B"]["status"] == "INDETERMINATE", (reports, output[-1500:])
    assert "did not come back under a new generation" in output + " ".join(reports["M40-PHYS-B"]["incidents"])
    timeline = json.loads((campaign(tmp_path, "M40-PHYS-B") / "m40-phys-b-timeline.json").read_text())
    assert timeline["phase"] == "return" and timeline["error"] == "ContinuityError"
    assert not posts(tmp_path), "no command, no reservation, no recovery attempt"


def test_a_gesture_that_is_never_made_is_blocked(sim, tmp_path):
    url, client, process = sim({}, control=True)
    code, output, reports, _ = run_phys(tmp_path, url, process, phys_session(tmp_path), act=None)
    assert reports["M40-PHYS-B"]["status"] == "BLOCKED" and "was not seen disconnecting" in " ".join(reports["M40-PHYS-B"]["incidents"])
    assert "OPERATOR>>" in output and not posts(tmp_path)


def test_a_re_registration_of_the_other_card_during_the_gesture_is_indeterminate(sim, tmp_path):
    url, client, process = sim({}, control=True)
    act = operator(process, down_s=1.0, then=(2.2, "board", 1.0))                    # B back after ~1.5 s, then A flaps during the settle time
    code, output, reports, _ = run_phys(tmp_path, url, process, phys_session(tmp_path, settle_s=5), act=act)
    assert code != 0 and reports["M40-PHYS-B"]["status"] == "INDETERMINATE", (reports, output[-1500:])
    assert "not attributable" in output + " ".join(reports["M40-PHYS-B"]["incidents"]), "a re-registration of A is a loss of continuity, never a PASS"


def test_phys_b_is_blocked_without_its_consent_or_section(sim, tmp_path):
    url, client, process = sim({}, control=True)
    for name, document in {"consent": phys_session(tmp_path, consents=()), "section": {**base_session(tmp_path, consents=("M40-PHYS-B",)),
                                                                                       "m40": {"sample_interval_s": 0.1}}}.items():
        work = tmp_path / name
        work.mkdir()
        code, output, reports, _ = run_phys(work, url, process, {**document, "evidence_root": str(work / "evidence")}, act=None)
        assert "BLOCKED" in output and "OPERATOR>>" not in output and not reports and not posts(work), name


ENV_PHYS_ID = {"SIM_IDENTITY": "{}", "SIM_IDENTIFY_EVENTS": "1"}


def test_phys_b_with_identity_ties_the_logical_id_the_cable_and_the_hardware(sim, tmp_path):
    url, client, process = sim(ENV_PHYS_ID, control=True)
    path = sim_reference(tmp_path)
    before = path.read_bytes()
    code, output, reports, _ = run_phys(tmp_path, url, process, phys_session(tmp_path, consents=("M40-PHYS-B", "M40-PHYS-B-ID")), act=operator(process))
    assert code == 0 and reports["M40-PHYS-B"]["status"] == "PASS", output[-2500:]
    text = " ".join(reports["M40-PHYS-B"]["evidence"])
    assert "PHYSICAL ASSOCIATION" in text and "HARDWARE IDENTITY (separate from the association)" in text and "the reference was not replaced" in text
    assert len(operations(tmp_path)) == 1, "exactly ONE identify, on B, after its return"
    folder = campaign(tmp_path, "M40-PHYS-B")
    rows = json.loads((folder / "m40-phys-b-fingerprints.json").read_text())["reads"]
    assert len(rows) == 1 and rows[0]["digest"] == sim_fingerprint("board2")["digest"] and rows[0]["loader"] and rows[0]["console"]
    assert path.read_bytes() == before, "the newly read identity never replaces the reference"
    assert client.device("board2")["reservation"] is None
    assert_no_raw_identity(tmp_path, output)


def test_another_hardware_behind_the_same_logical_id_is_a_fail_and_the_reference_stays(sim, tmp_path):
    url, client, process = sim({**ENV_PHYS_ID, "SIM_IDENTITY": json.dumps({"board2": {"mac": "de:ad:be:ef:00:66"}})}, control=True)
    path = sim_reference(tmp_path)
    before = path.read_bytes()
    code, output, reports, _ = run_phys(tmp_path, url, process, phys_session(tmp_path, consents=("M40-PHYS-B", "M40-PHYS-B-ID")), act=operator(process))
    assert code != 0 and reports["M40-PHYS-B"]["status"] == "FAIL" and "physical association broken" in output + " ".join(reports["M40-PHYS-B"]["incidents"])
    assert "mac" in output and path.read_bytes() == before
    assert_no_raw_identity(tmp_path, output)


def test_the_identity_option_needs_an_existing_reference_before_any_gesture(sim, tmp_path):
    url, client, process = sim(ENV_PHYS_ID, control=True)
    install_key(tmp_path)
    code, output, reports, _ = run_phys(tmp_path, url, process, phys_session(tmp_path, consents=("M40-PHYS-B", "M40-PHYS-B-ID")), act=operator(process))
    assert reports["M40-PHYS-B"]["status"] == "BLOCKED" and "never recorded from this run" in " ".join(reports["M40-PHYS-B"]["incidents"])
    assert "OPERATOR>>" not in output, "the operator is not asked to unplug anything when the comparison is impossible"
    assert not (tmp_path / "private" / "reference.json").exists() and not posts(tmp_path)


def test_an_identify_without_a_proven_console_return_after_the_gesture_is_indeterminate_and_keeps_the_reservation(sim, tmp_path):
    url, client, process = sim({**ENV_PHYS_ID, "SIM_NO_CONSOLE_RETURN": "board2"}, control=True)
    sim_reference(tmp_path)
    code, output, reports, _ = run_phys(tmp_path, url, process, phys_session(tmp_path, consents=("M40-PHYS-B", "M40-PHYS-B-ID")), act=operator(process))
    assert reports["M40-PHYS-B"]["status"] == "INDETERMINATE" and "no `console opened` event" in output + " ".join(reports["M40-PHYS-B"]["incidents"])
    assert len(operations(tmp_path)) == 1 and not deletes(tmp_path), "never repeated, never released on an unknown state"
    assert client.device("board2")["reservation"] is not None
    assert_no_raw_identity(tmp_path, output)


def test_an_error_during_the_association_keeps_the_evidence_and_is_not_masked(sim, tmp_path):
    url, client, process = sim({}, control=True)
    defect = ("\n_real_drain = drain_events\n_calls = []\n\n\ndef drain_events(*args, **kwargs):\n    _calls.append(1)\n    if len(_calls) > 3:\n"
              "        raise RuntimeError('boom during the association (injected)')\n    return _real_drain(*args, **kwargs)\n")
    code, output, reports, _ = run_with_defect(tmp_path, url, SELECT_PHYS, phys_session(tmp_path), defect, operator=operator(process))
    report = reports["M40-PHYS-B"]
    assert code != 0 and report["status"] == "FAIL" and "RuntimeError" in " ".join(report["incidents"])
    timeline = json.loads((campaign(tmp_path, "M40-PHYS-B") / "m40-phys-b-timeline.json").read_text())
    assert timeline["error"] == "RuntimeError" and timeline["phase"] in ("gesture", "return")
    assert (campaign(tmp_path, "M40-PHYS-B") / "m40-observed-card.jsonl").is_file() and not posts(tmp_path)


def test_a_phys_b_evidence_failure_never_replaces_the_error_in_flight(sim, tmp_path):
    url, client, process = sim({}, control=True)
    code, output, reports, _ = run_with_defect(tmp_path, url, SELECT_PHYS, phys_session(tmp_path), write_fails("m40-phys-b-timeline.json"),
                                               operator=operator(process, device="board"))
    report = reports["M40-PHYS-B"]
    assert report["status"] == "FAIL" and "wrong cable" in output + " ".join(report["incidents"]), "the wrong-cable failure stays the verdict"
    assert "m40-phys-b-timeline.json not written (OSError)" in " ".join(report["incidents"])
    folder = campaign(tmp_path, "M40-PHYS-B")
    assert (folder / "m40-phys-b-steps.jsonl").is_file() and (folder / "m40-observed-card.jsonl").is_file() and not (folder / "m40-phys-b-timeline.json").exists()


def test_every_raw_identify_result_is_cleared_right_after_its_fingerprint(sim, tmp_path):
    """The raw result is the only holder of the MAC in the client: it must be emptied for each of the four identify, before anything else."""
    url, client, _ = sim(ENV_ID)
    log = tmp_path / "scrub.log"
    defect = ("\n_real_scrub = scrub\n\n\ndef scrub(result):\n    with open(" + repr(str(log)) + ", 'a') as stream:\n"
              "        stream.write(str(len(result)) + ' ' + str('mac' in result) + '\\n')\n    return _real_scrub(result)\n")
    code, output, reports, _ = run_with_identity_defect(tmp_path, url, SELECT_ID, base_session(tmp_path), defect)
    assert reports["M40-ID"]["status"] == "PASS", output[-1200:]
    rows = log.read_text().splitlines()
    assert len(rows) == 4 and all(row.endswith("True") and int(row.split()[0]) >= 4 for row in rows), rows


DOUBLE_COUNT = ("\n_real_request = None\nimport iobewi_bench.qualification as _q\n_real_request = _q.ObservedClient._request\n\n\n"
                "def _counting_request(self, method, path, body=None, **kwargs):\n    if method == 'POST' and path == '/api/v1/operations':\n"
                "        self.submissions += 1\n    return _real_request(self, method, path, body, **kwargs)\n\n\n_q.ObservedClient._request = _counting_request\n")


def test_m40_id_counts_its_four_submissions(sim, tmp_path):
    url, client, _ = sim(ENV_ID)
    code, output, reports, _ = run_with_defect(tmp_path, url, SELECT_ID, base_session(tmp_path), DOUBLE_COUNT)
    assert reports["M40-ID"]["status"] == "FAIL" and "operations submitted, expected 4" in output + " ".join(reports["M40-ID"]["incidents"])
