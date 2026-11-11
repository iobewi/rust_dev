"""Hardware identity fingerprints for M40-ID and M40-PHYS-B (issue #40).

A card's hardware identity is (chip, MAC, flash_size, flash_id), as returned by `identify`. The MAC is the only part that is unique to a
unit, and a raw MAC must never reach a persisted proof. This module therefore reduces the raw result to a FINGERPRINT as early as possible:

  canonical form (version 1):   iobewi-identity-v1|chip=<chip>|mac=<12 lowercase hex>|flash_size=<int>|flash_id=0x<hex>
  digest:                       HMAC-SHA-256(key, canonical form), hex      (key: 32 random bytes, private, persistent, mode 0600)
  field digests (diagnostics):  chip, flash_size and flash_id in clear (they are model data, not unit data);
                                mac as HMAC-SHA-256(key, "mac|" + <12 hex>) so a change of ONE field can be named without keeping the MAC
  mac_suffix:                   the last two octets (4 hex), the only part of the MAC allowed in diagnostics

HMAC is used instead of SHA-256(key || text): it is the standard keyed construction over SHA-256 and has no length-extension weakness.
Nothing in this module returns, logs or puts in an exception message a raw MAC or the key. Hard limit, stated plainly: the raw MAC IS
present in the page agent, in the server's memory and in the HTTP response of the `identify` operation (the durable journal strips it:
`safe_result` has no `mac` field). This module cannot remove it from those components; it only keeps it out of everything the recipes persist.

The REFERENCE file records the fingerprints of the expected cards. It is created ONCE, atomically (a fully written 0600 temporary file is
hard-linked to its final name, which fails if the name exists), never overwritten, never partial (a crash leaves no final file) and is
independent of any campaign result directory.

PUBLICATION BOUNDARY (M40-ID): a reference is never created while a campaign can still fail. The recipe STAGES a candidate under the name
`<reference>.candidate` (same atomic, never-overwritten write; the candidate is not a reference: nothing ever loads it as one) and PROMOTES it
(hard link to the final name, then removal of the candidate) only after the campaign has closed with verdict PASS, i.e. after the explicit
release of the reservations, the final state checks and Campaign.close(). A campaign that ends FAIL, BLOCKED or INDETERMINATE discards the
candidate. "Qualified" (the verdict) and "published" (the reference exists) are two recorded facts; a failed promotion leaves no reference.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path

CANONICAL_VERSION = "iobewi-identity-v1"
REFERENCE_VERSION = 1
KEY_BYTES = 32
FIELDS = ("chip", "flash_size", "flash_id", "mac")
_CHIP = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
_MAC = re.compile(r"^[0-9a-f]{12}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class IdentityError(ValueError):
    """Raised for a malformed identity, key or reference. Messages never contain a raw MAC or key material."""


class ReferenceExists(IdentityError):
    pass


# --- fingerprints --------------------------------------------------------------------------------------------------
def normalize_mac(value):
    if not isinstance(value, str):
        raise IdentityError("the MAC is missing or is not a text")
    mac = value.strip().lower().replace(":", "").replace("-", "")
    if not _MAC.match(mac):
        raise IdentityError("the MAC is not 6 octets of hexadecimal")
    return mac


def canonical(chip, mac, flash_size, flash_id):
    if not isinstance(chip, str) or not _CHIP.match(chip):
        raise IdentityError("the chip name is missing or malformed")
    if type(flash_size) is not int or flash_size <= 0:
        raise IdentityError("flash_size must be a positive integer")
    if type(flash_id) is not int or flash_id < 0:
        raise IdentityError("flash_id must be a non-negative integer")
    return f"{CANONICAL_VERSION}|chip={chip}|mac={normalize_mac(mac)}|flash_size={flash_size}|flash_id={flash_id:#x}"


def _hmac(key, text):
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()


def fingerprint(result, key):
    """Reduce an `identify` result to its fingerprint. The caller must clear `result` afterwards (see `scrub`)."""
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
        raise IdentityError("the identity key must be 32 bytes")
    if not isinstance(result, dict):
        raise IdentityError("the identify result is not a dictionary")
    try:
        chip, mac, size, flash_id = result["chip"], result["mac"], result["flash_size"], result["flash_id"]
    except KeyError as error:
        raise IdentityError(f"the identify result has no {error.args[0]!r} field") from None
    mac = normalize_mac(mac)
    text = canonical(chip, mac, size, flash_id)
    return {"version": CANONICAL_VERSION, "digest": _hmac(bytes(key), text),
            "fields": {"chip": chip, "flash_size": size, "flash_id": f"{flash_id:#x}", "mac": _hmac(bytes(key), "mac|" + mac)},
            "mac_suffix": mac[-4:]}


def scrub(result):
    """Drop the raw identity from the object that carried it (the Board keeps the same dictionary as `last_operation['result']`)."""
    if isinstance(result, dict):
        result.clear()


def different_fields(first, second):
    return [name for name in FIELDS if first["fields"][name] != second["fields"][name]]


def check_stable(reads, name="card"):
    """All reads of ONE card agree on the four fields. Needs at least two reads: a single read proves nothing about stability."""
    if len(reads) < 2:
        return [f"{name}: {len(reads)} read(s), at least 2 are needed to show stability"]
    problems = []
    for index, read in enumerate(reads[1:], start=2):
        changed = different_fields(reads[0], read)
        if changed:
            problems.append(f"{name}: read {index} differs from read 1 in {changed}")
        elif read["digest"] != reads[0]["digest"]:
            problems.append(f"{name}: read {index} has another digest although no field differs")
    return problems


def check_distinct(first, second, names=("first", "second")):
    """Two different physical cards: different digests AND different MAC digests (an equal flash_id alone is normal for one model)."""
    problems = []
    if first["digest"] == second["digest"]:
        problems.append(f"{names[0]} and {names[1]} report the same hardware identity")
    if first["fields"]["mac"] == second["fields"]["mac"]:
        problems.append(f"{names[0]} and {names[1]} report the same MAC")
    return problems


# --- key ------------------------------------------------------------------------------------------------------------
def _require_private_file(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise IdentityError(f"{Path(path).name} is not a regular file (links are refused)")
    if info.st_mode & 0o077:
        raise IdentityError(f"{Path(path).name} is readable by group or others (mode {stat.S_IMODE(info.st_mode):o}); expected 0600")
    if info.st_uid != os.geteuid():
        raise IdentityError(f"{Path(path).name} is not owned by the current user")
    return info


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _require_private_directory(directory):
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode):
        raise IdentityError(f"{Path(directory).name} is not a directory (links are refused)")
    if info.st_mode & 0o077:
        raise IdentityError(f"{Path(directory).name} is accessible to group or others (mode {stat.S_IMODE(info.st_mode):o}); expected 0700")


def create_key(path):
    """Create the key ONCE: O_EXCL, no link following, mode 0600 from the first byte, CSPRNG, fsynced. Never replaces a key."""
    path = Path(path)
    _require_private_directory(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        data = secrets.token_bytes(KEY_BYTES)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    _sync_directory(path.parent)


def load_key(path):
    path = Path(path)
    info = _require_private_file(path)
    if info.st_size != KEY_BYTES:
        raise IdentityError(f"{path.name} does not hold {KEY_BYTES} bytes")
    with open(path, "rb") as stream:
        data = stream.read(KEY_BYTES + 1)
    if len(data) != KEY_BYTES:
        raise IdentityError(f"{path.name} does not hold {KEY_BYTES} bytes")
    return data


# --- reference ------------------------------------------------------------------------------------------------------
def reference_document(fingerprints, *, created=None):
    """The reference of a COMPLETE set of cards: {logical id: fingerprint}. The raw MAC is not in it, only digests and the 2-octet suffix."""
    if not fingerprints:
        raise IdentityError("a reference needs at least one card")
    return {"version": REFERENCE_VERSION, "canonical": CANONICAL_VERSION,
            "created": created or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cards": {name: {"digest": fp["digest"], "fields": dict(fp["fields"]), "mac_suffix": fp["mac_suffix"]} for name, fp in sorted(fingerprints.items())}}


def validate_reference(document, expected_cards):
    """A reference is valid only when complete: right version, one entry per expected card, every field present and well formed."""
    if not isinstance(document, dict) or document.get("version") != REFERENCE_VERSION or document.get("canonical") != CANONICAL_VERSION:
        raise IdentityError("the reference has an unknown version or canonical format")
    cards = document.get("cards")
    if not isinstance(cards, dict) or set(cards) != set(expected_cards):
        raise IdentityError("the reference does not hold exactly the expected cards (a partial reference is never valid)")
    for name, entry in cards.items():
        fields = entry.get("fields") if isinstance(entry, dict) else None
        if (not isinstance(fields, dict) or set(fields) != set(FIELDS) or not _HEX64.match(str(entry.get("digest", "")))
                or not _HEX64.match(str(fields.get("mac", ""))) or not re.match(r"^[0-9a-f]{4}$", str(entry.get("mac_suffix", "")))):
            raise IdentityError(f"the reference entry of {name} is incomplete or malformed")
    return document


def write_reference(path, document):
    """Create the reference ONCE and atomically. Raises ReferenceExists if the name is taken (never overwritten). A crash at any point leaves
    either no final file or a complete one: the final name only ever appears through a hard link to a fully written, fsynced 0600 file."""
    path = Path(path)
    _require_private_directory(path.parent)
    data = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise ReferenceExists(f"{path.name} already exists: a reference is never overwritten") from None
        _sync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load_reference(path, expected_cards):
    path = Path(path)
    _require_private_file(path)
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise IdentityError(f"{path.name} cannot be read as a reference ({type(error).__name__})") from None
    return validate_reference(document, expected_cards)


def compare_reference(reference, fingerprints):
    """Per card: the fingerprint read now against the reference, naming the differing fields (never replacing the reference)."""
    problems = []
    for name, current in sorted(fingerprints.items()):
        recorded = reference["cards"].get(name)
        if recorded is None:
            problems.append(f"{name}: no entry in the reference")
        elif recorded["digest"] != current["digest"]:
            changed = [field for field in FIELDS if recorded["fields"][field] != current["fields"][field]]
            problems.append(f"{name}: hardware identity differs from the reference in {changed or ['digest only']}")
    return problems


# --- publication boundary: stage, then promote only after a PASS ------------------------------------------------------
def candidate_path(path):
    path = Path(path)
    return path.with_name(path.name + ".candidate")


def has_candidate(path):
    candidate = candidate_path(path)
    return candidate.exists() or candidate.is_symlink()


def stage_reference(path, document):
    """Write the CANDIDATE once, atomically and privately (same guarantees as write_reference; a stale candidate raises ReferenceExists).
    The candidate is not a reference: `load_reference` is only ever called on the final name."""
    candidate = candidate_path(path)
    write_reference(candidate, document)
    return candidate


def promote_reference(path):
    """Publish a staged candidate as THE reference: a hard link to the final name (fails with ReferenceExists if the name is taken: never
    overwritten), then the candidate is removed. The final name only ever appears complete."""
    path, candidate = Path(path), candidate_path(path)
    _require_private_file(candidate)
    try:
        os.link(candidate, path)
    except FileExistsError:
        raise ReferenceExists(f"{path.name} already exists: a reference is never overwritten") from None
    _sync_directory(path.parent)
    try:
        os.unlink(candidate)
    except FileNotFoundError:
        pass


def discard_candidate(path):
    """Remove a staged candidate (campaign not PASS, or promotion failed). Returns True if one was removed."""
    try:
        os.unlink(candidate_path(path))
        return True
    except FileNotFoundError:
        return False
