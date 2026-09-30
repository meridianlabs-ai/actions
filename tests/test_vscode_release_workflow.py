"""Tests for the publish job of release-please-vscode.yml: what binds the .vsix to the release.

The build job runs the consuming repo's code (install hooks, scripts, tests,
vsce:package), so everything it produces, the .vsix, its file name and its
version output, is repo-controlled. vsce and ovsx publish to whatever
publisher, name and version the package's own manifests declare, with PATs
that reach every extension the account manages. The publish job's Verify VSIX
step is therefore the control: before any step holds a PAT it must open the
downloaded package without executing anything in it and fail unless
extension/package.json and extension.vsixmanifest agree and name exactly the
caller's `extension-id` at the version in the release-please tag. Python's
zipfile and yauzl (the reader vsce publishes with) do not find a zip's central
directory the same way, so the validator checks the zip structure itself
before zipfile reads it, and a later step, still before any PAT, reads the
package with vsce's own readVSIXPackage and fails unless it says the same.

The validator is inline in the workflow (a Python heredoc), so the tests lift
it from the YAML and exercise the real code: in-process against fixture
packages built here (intended releases in each tag shape the callers'
release-please configs produce, swapped identities, tag mismatches,
disagreeing manifests, ambiguous or unsafe archives, archives with two
central directories, malformed inputs), and end to end by running the publish
job's `run:` steps in order under bash with stand-ins for npm, vsce (and its
reader module), ovsx and gh on PATH that record what they were asked to do. No
package is published and no real PAT exists here. Where the real vsce of the
workflow's default `vsce-version` is on PATH (`.github/workflows/tests.yml`
installs it), the vsce-reader tests also package an extension with it and run
the cross-check step against its real reader; elsewhere they skip, and on a
GitHub runner they fail instead.

Run with `python3 -m pytest` from the repo root (needs pytest and PyYAML;
`.github/workflows/tests.yml` does the same in CI). jq and node must be on
PATH, as they are on the hosted runners.
"""

from __future__ import annotations

import json
import os
import io
import re
import shutil
import struct
import subprocess
import sys
import types
import warnings
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-please-vscode.yml"

PUBLISHER, NAME, VERSION = "ukaisi", "inspect-ai", "0.9.19"
EXTENSION_ID = f"{PUBLISHER}.{NAME}"
TAG = f"v{VERSION}"
FILENAME = f"{NAME}-{VERSION}.vsix"
VSX_NS = "http://schemas.microsoft.com/developer/vsx-schema/2011"

VERIFY = "Verify VSIX"
INSTALL = "Install marketplace CLIs"
CROSSCHECK = "Cross-check VSIX with vsce's reader"
MARKETPLACE = "Publish to VS Code Marketplace"
OPENVSX = "Publish to Open VSX"
UPLOAD = "Upload VSIX to GitHub release"


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def publish_steps() -> list[dict]:
    return workflow()["jobs"]["publish"]["steps"]


def step(name: str) -> dict:
    for s in publish_steps():
        if s.get("id") == name or s.get("name") == name:
            return s
    raise KeyError(name)


def validator_source() -> str:
    """The Python program the Verify VSIX step feeds to `python3 -`."""
    m = re.fullmatch(r"python3 - <<'PY'\n(.*)\nPY\n", step(VERIFY)["run"], re.S)
    assert m, "Verify VSIX must be a single `python3 - <<'PY' ... PY` heredoc"
    return m.group(1)


def load_validator() -> types.ModuleType:
    module = types.ModuleType("verify_vsix")
    exec(compile(validator_source(), f"{WORKFLOW}#{VERIFY}", "exec"), module.__dict__)
    return module


validator = load_validator()
Rejected = validator.Rejected


# --- Fixture packages ------------------------------------------------------------------


def package_json(publisher: str = PUBLISHER, name: str = NAME, version: str = VERSION, **extra) -> bytes:
    manifest = {"name": name, "displayName": "Inspect AI", "publisher": publisher, "version": version, "engines": {"vscode": "^1.90.0"}, **extra}
    return json.dumps(manifest, indent=2).encode()


def vsixmanifest(publisher: str = PUBLISHER, id: str = NAME, version: str = VERSION, *, identity_attrs: str | None = None, before_metadata: str = "", before_identity: str = "", installation_extra: str = "") -> bytes:
    attrs = identity_attrs if identity_attrs is not None else f'Language="en-US" Id="{id}" Version="{version}" Publisher="{publisher}"'
    return f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="{VSX_NS}" xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  {before_metadata}<Metadata>
    {before_identity}<Identity {attrs}/>
    <DisplayName>Inspect AI</DisplayName>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/>{installation_extra}</Installation>
  <Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets>
</PackageManifest>
""".encode()


CONTENT_TYPES = b'<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension=".json" ContentType="application/json"/></Types>'


def entries(pkg: bytes | None = None, manifest: bytes | None = None) -> list[tuple[str, bytes]]:
    """The layout vsce writes: the XML manifest and content types at the root, the extension under extension/."""
    return [
        ("extension.vsixmanifest", vsixmanifest() if manifest is None else manifest),
        ("[Content_Types].xml", CONTENT_TYPES),
        ("extension/package.json", package_json() if pkg is None else pkg),
        ("extension/README.md", b"# Inspect AI\n"),
        ("extension/dist/extension.js", b"module.exports = {};\n"),
    ]


def write_vsix(directory: Path, items: list[tuple[str | zipfile.ZipInfo, bytes]] | bytes | None = None, filename: str = FILENAME) -> Path:
    """Write a package from (name, data) items, or the given archive bytes as they are."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_bytes(items if isinstance(items, bytes) else zip_bytes(items))
    return path


def zip_bytes(items: list[tuple[str | zipfile.ZipInfo, bytes]] | None = None, stream=None, comment: bytes = b"") -> bytes:
    buf = io.BytesIO() if stream is None else stream
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns about duplicate names; some tests write them on purpose
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in entries() if items is None else items:
                zf.writestr(name, data)
            zf.comment = comment
    return buf.getvalue()


class Unseekable(io.BytesIO):
    def seek(self, *args):
        raise OSError("not seekable")


def streamed_zip(items=None) -> bytes:
    """The archive zipfile writes to a stream it cannot seek back in: bit 3 set, zeros in each local header and a data descriptor after the data, as vsce writes the files it streams from disk."""
    return zip_bytes(items, stream=Unseekable())


def consistent(publisher: str = PUBLISHER, name: str = NAME, version: str = VERSION) -> list[tuple[str, bytes]]:
    """A well-formed package whose two manifests agree on the given identity."""
    return entries(package_json(publisher, name, version), vsixmanifest(publisher, name, version))


def with_extra(name: str, extra: bytes) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.extra = extra
    return info


def unicode_path_alias(name: str, alias: str) -> zipfile.ZipInfo:
    """An entry named `name` in its headers that yauzl (vsce's reader) renames to `alias` via an Info-ZIP Unicode Path field."""
    payload = b"\x01" + struct.pack("<I", zlib.crc32(name.encode())) + alias.encode()
    return with_extra(name, struct.pack("<HH", 0x7075, len(payload)) + payload)


# The intended package plus a second copy of each manifest, named differently in
# the zip headers but carrying Unicode Path fields that alias the manifest names.
ALIASED_ENTRIES = entries() + [
    (unicode_path_alias("extension/alternate.json", "extension/package.json"), package_json(name="other-extension")),
    (unicode_path_alias("alternate.vsixmanifest", "extension.vsixmanifest"), vsixmanifest(id="other-extension")),
]
# --- Archives with a second central directory (Claude Security finding 4773470) -------------------


def split_zip(data: bytes) -> tuple[bytes, bytes, int]:
    """(everything before the central directory, the central directory, its entry count) of an archive without a comment."""
    count, size, offset = struct.unpack("<10xHII", data[-22:-2])
    return data[:offset], data[offset : offset + size], count


def end_record(count: int, size: int, offset: int) -> bytes:
    return struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, count, count, size, offset, 0)


def zip64_end_record(count: int, size: int, offset: int) -> bytes:
    return struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, size, offset)


def shift_offsets(cd: bytes, delta: int, at: int = 0) -> bytes:
    """The central directory with every local header offset from `at` on moved by delta."""
    out, pos = bytearray(cd), 0
    while pos < len(out):
        n, m, k = struct.unpack_from("<HHH", out, pos + 28)
        offset = struct.unpack_from("<I", out, pos + 42)[0]
        struct.pack_into("<I", out, pos + 42, offset + delta if offset >= at else offset)
        pos += 46 + n + m + k
    return bytes(out)


def dual_directory(sentinel: bool) -> bytes:
    """One archive with two central directories: the intended package's for Python's zipfile, another extension's for yauzl.

    CPython's zipfile follows the Zip64 locator to the Zip64 end record just
    before it, which names the intended directory (3.9.6, 3.12.11 and 3.13.9
    do; 3.12.13 and 3.14.7 refuse the layout instead). yauzl 3 (vsce
    3.9.2's reader) follows the locator's offset field to the other Zip64 end
    record; yauzl 2 follows a locator only when the end record holds the Zip64
    sentinels and otherwise uses the end record's own fields. Both lead to the
    other directory. `sentinel` writes 0xFFFF/0xFFFFFFFF into the end record.
    """
    local_a, cd_a, count_a = split_zip(zip_bytes())
    local_b, cd_b, count_b = split_zip(zip_bytes(consistent(name="other-extension")))
    cd_b = shift_offsets(cd_b, len(local_a))
    cd_b_at = len(local_a) + len(local_b)
    zip64_b_at = cd_b_at + len(cd_b)
    cd_a_at = zip64_b_at + 56
    body = local_a + local_b + cd_b + zip64_end_record(count_b, len(cd_b), cd_b_at) + cd_a + zip64_end_record(count_a, len(cd_a), cd_a_at)
    body += struct.pack("<4sIQI", b"PK\x06\x07", 0, zip64_b_at, 1)
    if sentinel:
        return body + struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    return body + end_record(count_b, len(cd_b), cd_b_at)


DUAL_LOCATOR = dual_directory(sentinel=False)
DUAL_SENTINEL = dual_directory(sentinel=True)

FOREIGN_METADATA = vsixmanifest(before_metadata='<Metadata xmlns="urn:other"><Identity Id="other-extension" Version="0.9.20" Publisher="ukaisi"/></Metadata>')
FOREIGN_IDENTITY = vsixmanifest(before_identity='<Identity xmlns="urn:other" Id="other-extension" Version="0.9.20" Publisher="ukaisi"/>')


def rejected(tmp_path: Path, items=None, *, extension_id: str = EXTENSION_ID, tag: str = TAG, filename: str = FILENAME) -> str:
    write_vsix(tmp_path / "vsix", items, filename)
    with pytest.raises(Rejected) as info:
        validator.verify(str(tmp_path / "vsix"), extension_id, tag)
    return str(info.value)


# --- Intended releases pass --------------------------------------------------------------


@pytest.mark.parametrize(
    "tag",
    [
        "v0.9.19",  # release-please default
        "0.9.19",  # include-v-in-tag: false (gen-release-please-config.sh `bare`)
        "inspect-ai-v0.9.19",  # include-component-in-tag (release-please's node default in manifest mode)
        "inspect-ai-0.9.19",  # both
    ],
)
def test_intended_release_passes_in_each_tag_shape(tmp_path, tag):
    write_vsix(tmp_path / "vsix")
    path, version = validator.verify(str(tmp_path / "vsix"), EXTENSION_ID, tag)
    assert (path, version) == (str(tmp_path / "vsix" / FILENAME), VERSION)


def test_first_release_of_a_new_version_passes_when_manifests_and_tag_agree(tmp_path):
    write_vsix(tmp_path / "vsix", consistent(version="1.0.0"), "inspect-ai-1.0.0.vsix")
    assert validator.verify(str(tmp_path / "vsix"), EXTENSION_ID, "v1.0.0")[1] == "1.0.0"


def test_file_name_is_not_evidence_but_must_be_plain(tmp_path):
    # A name that does not match the contents is fine: the manifests decide.
    write_vsix(tmp_path / "vsix", filename="other-0.0.1.vsix")
    assert validator.verify(str(tmp_path / "vsix"), EXTENSION_ID, TAG)[1] == VERSION


# --- The package names something this release may not publish ------------------------------


@pytest.mark.parametrize(
    "publisher, name, version",
    [
        (PUBLISHER, "other-extension", VERSION),  # another extension of the same publisher
        ("other-publisher", NAME, VERSION),  # another publisher the PAT may reach
        ("other-publisher", "other-extension", VERSION),
        (PUBLISHER, NAME, "0.9.20"),  # a version the tag does not authorize
        (PUBLISHER, NAME, "0.9.1"),  # a prefix of the released version
        (PUBLISHER.upper(), NAME, VERSION),  # identity is compared exactly
    ],
)
def test_rejects_consistent_package_for_the_wrong_identity(tmp_path, publisher, name, version):
    message = rejected(tmp_path, consistent(publisher, name, version))
    assert "may only publish ukaisi.inspect-ai 0.9.19" in message
    assert f"{publisher}.{name} {version}" in message


@pytest.mark.parametrize("tag", ["v0.9.20", "0.9.18", "inspect-ai-v1.0.0"])
def test_rejects_package_whose_version_is_not_the_tags(tmp_path, tag):
    assert "may only publish" in rejected(tmp_path, tag=tag)


@pytest.mark.parametrize("extension_id", ["ukaisi.other-extension", "other.inspect-ai"])
def test_rejects_package_that_is_not_the_extension_id(tmp_path, extension_id):
    assert "may only publish" in rejected(tmp_path, extension_id=extension_id)


@pytest.mark.parametrize(
    "pkg, manifest",
    [
        (package_json(publisher="other-publisher"), vsixmanifest()),
        (package_json(), vsixmanifest(publisher="other-publisher")),
        (package_json(name="other-extension"), vsixmanifest()),
        (package_json(), vsixmanifest(id="other-extension")),
        (package_json(version="0.9.20"), vsixmanifest()),
        (package_json(), vsixmanifest(version="0.9.20")),
    ],
)
def test_rejects_manifests_that_disagree_with_each_other(tmp_path, pkg, manifest):
    # vsce and ovsx read package.json; the registries also read the XML. Both must say the same thing.
    assert "says" in rejected(tmp_path, entries(pkg, manifest))


# --- Archives that could make two consumers read different manifests -------------------------


def test_rejects_case_variant_manifest_entry(tmp_path):
    # vsce matches entry names case-insensitively; ovsx and the registries do not.
    items = [(n.replace("extension/package.json", "Extension/Package.json"), d) for n, d in entries()]
    assert "would be read as extension/package.json by vsce" in rejected(tmp_path, items)


def test_rejects_archive_with_manifest_and_case_variant_of_it(tmp_path):
    items = entries() + [("EXTENSION/PACKAGE.JSON", package_json(name="other-extension"))]
    assert "differ only in case" in rejected(tmp_path, items)


@pytest.mark.parametrize("name", ["extension/package.json", "extension.vsixmanifest"])
def test_rejects_repeated_manifest_entry(tmp_path, name):
    # Consumers disagree on whether the first or the last repeated entry wins.
    items = entries() + [(name, package_json(name="other-extension") if name.endswith(".json") else vsixmanifest(id="other-extension"))]
    assert "repeat or differ only in case" in rejected(tmp_path, items)


def test_rejects_repeated_entry_anywhere(tmp_path):
    items = entries() + [("extension/README.md", b"again")]
    assert "repeat" in rejected(tmp_path, items)


@pytest.mark.parametrize("name", ["../package.json", "/extension/package.json", "extension\\package.json", "C:extension/package.json", "extension//package.json", "./extension/package.json", "extension/../package.json"])
def test_rejects_unsafe_entry_names(tmp_path, name):
    assert "unsafe archive entry name" in rejected(tmp_path, entries() + [(name, b"{}")])


def test_accepts_directory_entries(tmp_path):
    write_vsix(tmp_path / "vsix", [("extension/", b""), ("extension/dist/", b"")] + entries())
    assert validator.verify(str(tmp_path / "vsix"), EXTENSION_ID, TAG)[1] == VERSION


def test_rejects_symlink_entry(tmp_path):
    link = zipfile.ZipInfo("extension/link")
    link.external_attr = (0o120777 << 16) | 0x20
    assert "is a symlink" in rejected(tmp_path, entries() + [(link, b"../../outside")])


@pytest.mark.parametrize(
    "name, alias",
    [("extension/alternate.json", "extension/package.json"), ("alternate.vsixmanifest", "extension.vsixmanifest"), ("extension/dist/x.js", "extension/dist/y.js")],
)
def test_rejects_unicode_path_extra_field_that_renames_an_entry_for_yauzl(tmp_path, name, alias):
    # yauzl applies the field (as does Python's zipfile from 3.12); Java's ZipFile and older
    # Python keep the header name, so the entry is one file to one consumer and the manifest
    # to another. The message names both, whichever Python runs the validator.
    message = rejected(tmp_path, entries() + [(unicode_path_alias(name, alias), package_json(name="other-extension"))])
    assert f"archive entry {name!r} carries a Unicode Path extra field naming it {alias!r}" in message


def test_rejects_both_manifests_aliased_at_once(tmp_path):
    assert "Unicode Path extra field" in rejected(tmp_path, ALIASED_ENTRIES)


@pytest.mark.parametrize("extra", [b"\x75\x70", struct.pack("<HH", 0x5455, 9) + b"\x01\x00\x00\x00\x00"])
def test_rejects_extra_field_data_that_does_not_parse(tmp_path, extra):
    # Python's zipfile refuses some corrupt extra fields itself while listing the archive; the validator refuses the rest.
    message = rejected(tmp_path, entries() + [(with_extra("extension/dist/x.js", extra), b"")])
    assert "malformed extra-field data" in message or "not a readable zip archive" in message


def test_accepts_ordinary_extra_fields(tmp_path):
    timestamp = struct.pack("<HHBI", 0x5455, 5, 1, 1_700_000_000)  # Info-ZIP extended timestamp
    write_vsix(tmp_path / "vsix", entries() + [(with_extra("extension/dist/x.js", timestamp), b"")])
    assert validator.verify(str(tmp_path / "vsix"), EXTENSION_ID, TAG)[1] == VERSION


@pytest.mark.parametrize("missing", ["extension/package.json", "extension.vsixmanifest"])
def test_rejects_missing_manifest(tmp_path, missing):
    assert f"{missing} is missing" in rejected(tmp_path, [(n, d) for n, d in entries() if n != missing])


def test_rejects_non_zip(tmp_path):
    (tmp_path / "vsix").mkdir()
    (tmp_path / "vsix" / FILENAME).write_bytes(b"#!/bin/sh\necho not a zip\n")
    with pytest.raises(Rejected, match="not an end-of-central-directory record"):
        validator.verify(str(tmp_path / "vsix"), EXTENSION_ID, TAG)


def test_rejects_oversized_manifest(tmp_path):
    big = package_json(padding="x" * (1 << 20))
    assert "larger than" in rejected(tmp_path, entries(pkg=big))


# --- Zip structure: one central directory, the same for every reader ---------------------------

HONEST = zip_bytes()
END = len(HONEST) - 22
LOCAL, CENTRAL, COUNT = split_zip(HONEST)


def patched(data: bytes, offset: int, fmt: str, *values) -> bytes:
    out = bytearray(data)
    struct.pack_into(fmt, out, offset, *values)
    return bytes(out)


def local_header(data: bytes, name: str) -> int:
    return zipfile.ZipFile(io.BytesIO(data)).getinfo(name).header_offset


def central_record(data: bytes, name: str) -> int:
    return data.index(name.encode(), len(split_zip(data)[0])) - 46


def with_comment(name: str, comment: bytes) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.comment = comment  # a file comment lives in the central directory only
    return info


@pytest.mark.parametrize(
    "data, reason",
    [(DUAL_LOCATOR, "does not end at the end-of-central-directory record"), (DUAL_SENTINEL, "the end-of-central-directory record defers to Zip64 records")],
    ids=["zip64-locator-without-sentinels", "zip64-sentinels"],
)
def test_rejects_the_findings_two_central_directory_layouts(tmp_path, data, reason):
    # Refused by the structural checks, before zipfile reads anything, whichever Python runs the validator.
    assert reason in rejected(tmp_path, data)


def test_honest_archive_passes_the_structural_checks():
    names = [r[0] for r in validator.check_structure(HONEST)]
    assert names == [n.encode() for n, _ in entries()]


@pytest.mark.parametrize(
    "data, reason",
    [
        (zip_bytes(comment=b"note"), "last 22 bytes are not an end-of-central-directory record"),
        (HONEST + b"\0" * 22, "last 22 bytes are not an end-of-central-directory record"),
        (patched(HONEST, END + 20, "<H", 1), "has a comment or names more than one disk"),  # a comment length with no comment
        (patched(HONEST, END + 4, "<H", 1), "names more than one disk"),
        (patched(HONEST, END + 6, "<H", 1), "names more than one disk"),
        (patched(HONEST, END + 8, "<H", COUNT + 1), "names more than one disk"),  # this disk's count is not the total
        (patched(HONEST, END + 8, "<HH", COUNT - 1, COUNT - 1), f"counts {COUNT - 1} entries but the central directory holds {COUNT}"),
        (patched(HONEST, END + 8, "<HH", COUNT + 1, COUNT + 1), f"counts {COUNT + 1} entries but the central directory holds {COUNT}"),
        (patched(HONEST, END + 12, "<I", 0xFFFFFFFF), "defers to Zip64 records"),
        (patched(HONEST, END + 16, "<I", 0xFFFFFFFF), "defers to Zip64 records"),
        # zipfile reads a directory that does not end at the end record from where it would end (it
        # assumes prepended data); yauzl reads it at the offset the end record names
        (b"\0" * 16 + HONEST, "does not end at the end-of-central-directory record"),
        (LOCAL + CENTRAL + b"\0" * 16 + end_record(COUNT, len(CENTRAL), len(LOCAL)), "does not end at the end-of-central-directory record"),
        (patched(HONEST, len(LOCAL), "<4s", b"PK\x01\x03"), f"malformed central-directory record at offset {len(LOCAL)}"),
        (patched(HONEST, central_record(HONEST, "extension/README.md") + 20, "<I", 0xFFFFFFFF), "central-directory record for b'extension/README.md' defers to Zip64 fields"),
        (patched(HONEST, central_record(HONEST, "extension/README.md") + 42, "<I", 0xFFFFFFFF), "defers to Zip64 fields"),
        (patched(HONEST, central_record(HONEST, "extension/README.md") + 34, "<H", 1), "defers to Zip64 fields"),
    ],
    ids=["zip-comment", "trailing-data", "comment-length", "disk-number", "directory-disk", "disk-count", "fewer-entries-counted", "more-entries-counted", "size-sentinel", "offset-sentinel", "prepended-data", "gap-before-end-record", "bad-record-signature", "entry-size-sentinel", "entry-offset-sentinel", "entry-disk"],
)
def test_rejects_end_record_and_central_directory_that_readers_could_resolve_differently(tmp_path, data, reason):
    assert reason in rejected(tmp_path, data)


def with_gap(at: int, gap: bytes) -> bytes:
    """The honest archive with `gap` inserted at offset `at` of its entry area, the directory and end record adjusted to match."""
    central = shift_offsets(CENTRAL, len(gap), at)
    return LOCAL[:at] + gap + LOCAL[at:] + central + end_record(COUNT, len(central), len(LOCAL) + len(gap))


@pytest.mark.parametrize(
    "signature, reason",
    [
        (b"PK\x06\x07", "a Zip64 end-of-central-directory locator signature"),
        (b"PK\x06\x06", "a Zip64 end-of-central-directory signature"),
        (b"PK\x05\x06", "a second end-of-central-directory signature"),
    ],
)
@pytest.mark.parametrize("where", ["first-entry-comment", "last-entry-comment-at-locator-position", "before-first-entry", "between-entries", "before-directory"])
def test_rejects_zip64_and_second_end_signatures_outside_entry_data(tmp_path, signature, reason, where):
    if where == "first-entry-comment":
        data = zip_bytes([(with_comment("extension.vsixmanifest", signature + b" in a comment"), vsixmanifest())] + entries()[1:])
    elif where == "last-entry-comment-at-locator-position":
        # the last record's comment ends 20 bytes before the end record, where zipfile looks for a Zip64 locator
        data = zip_bytes(entries() + [(with_comment("extension/z.txt", signature + b"\0" * 16), b"")])
    elif where == "before-first-entry":
        data = with_gap(0, b"gap " + signature)
    elif where == "between-entries":
        data = with_gap(local_header(HONEST, "extension/package.json"), b"gap " + signature)
    else:
        data = with_gap(len(LOCAL), b"gap " + signature)
    assert f"the archive contains {reason} at offset" in rejected(tmp_path, data)


def test_gaps_without_signatures_pass():
    # The gap alone is not what the scan refuses.
    for at in (0, local_header(HONEST, "extension/package.json"), len(LOCAL)):
        assert len(validator.check_structure(with_gap(at, b"gap bytes"))) == COUNT


@pytest.mark.parametrize("signature", [b"PK\x06\x06", b"PK\x06\x07", b"PK\x05\x06"])
def test_rejects_those_signatures_in_trailing_data(tmp_path, signature):
    assert "last 22 bytes are not an end-of-central-directory record" in rejected(tmp_path, HONEST + signature)


@pytest.mark.parametrize("signature", [b"PK\x06\x06", b"PK\x06\x07", b"PK\x05\x06"])
def test_accepts_those_signatures_inside_entry_data(tmp_path, signature):
    # Compressed or stored bytes can match by chance, or a bundled zip holds them; no reader looks
    # for these records there (decision: Ransom, 2026-09-30).
    stored = zipfile.ZipInfo("extension/dist/payload.bin")
    stored.compress_type = zipfile.ZIP_STORED
    write_vsix(tmp_path / "vsix", entries() + [(stored, b"data " + signature + b" data")])
    assert validator.verify(str(tmp_path / "vsix"), EXTENSION_ID, TAG)[1] == VERSION


def test_accepts_a_bundled_zip_stored_as_entry_data(tmp_path):
    nested = zipfile.ZipInfo("extension/dist/nested.zip")
    nested.compress_type = zipfile.ZIP_STORED
    write_vsix(tmp_path / "vsix", entries() + [(nested, DUAL_LOCATOR)])
    assert validator.verify(str(tmp_path / "vsix"), EXTENSION_ID, TAG)[1] == VERSION


@pytest.mark.parametrize("name", ["extension/package.json", "extension.vsixmanifest", "extension/README.md"])
def test_rejects_local_header_that_names_the_entry_differently(tmp_path, name):
    at = local_header(HONEST, name) + 30
    data = patched(HONEST, at + len(name) - 1, "<c", b"X")
    assert f"archive entry {name.encode()!r} is named {(name[:-1] + 'X').encode()!r} in its local header" in rejected(tmp_path, data)


@pytest.mark.parametrize("name", ["extension/package.json", "extension.vsixmanifest"])
@pytest.mark.parametrize("field, offset", [("crc", 14), ("compressed size", 18), ("size", 22)])
def test_rejects_manifest_local_header_whose_crc_or_sizes_differ(tmp_path, name, field, offset):
    at = local_header(HONEST, name) + offset
    data = patched(HONEST, at, "<I", struct.unpack_from("<I", HONEST, at)[0] + 1)
    assert f"{name}'s local header does not match its central-directory record" in rejected(tmp_path, data)


def test_accepts_data_descriptors_as_vsce_writes_them(tmp_path):
    data = streamed_zip()
    assert all(i.flag_bits & 8 for i in zipfile.ZipFile(io.BytesIO(data)).infolist())
    write_vsix(tmp_path / "vsix", data)
    assert validator.verify(str(tmp_path / "vsix"), EXTENSION_ID, TAG)[1] == VERSION


def descriptor(data: bytes, name: str) -> int:
    """Offset of the data descriptor that follows the named entry's data."""
    at = local_header(data, name)
    n, m = struct.unpack_from("<HH", data, at + 26)
    return at + 30 + n + m + zipfile.ZipFile(io.BytesIO(data)).getinfo(name).compress_size


@pytest.mark.parametrize("name", ["extension/package.json", "extension.vsixmanifest"])
@pytest.mark.parametrize("change", ["descriptor-size", "descriptor-signature", "local-header-bit-3", "local-header-size"])
def test_rejects_manifest_data_descriptor_that_differs(tmp_path, name, change):
    data = streamed_zip()
    if change == "descriptor-size":
        at = descriptor(data, name) + 12
        data = patched(data, at, "<I", struct.unpack_from("<I", data, at)[0] + 1)
    elif change == "descriptor-signature":
        data = patched(data, descriptor(data, name), "<4s", b"PK\x07\x09")
    elif change == "local-header-bit-3":
        data = patched(data, local_header(data, name) + 6, "<H", struct.unpack_from("<H", data, local_header(data, name) + 6)[0] & ~8)
    else:
        data = patched(data, local_header(data, name) + 22, "<I", 7)
    assert f"{name}'s local header does not match its central-directory record" in rejected(tmp_path, data)


def test_rejects_entry_without_local_header(tmp_path):
    data = patched(HONEST, central_record(HONEST, "extension/README.md") + 42, "<I", 1)
    assert "archive entry b'extension/README.md' has no local header at offset 1" in rejected(tmp_path, data)


def test_rejects_entry_that_runs_into_the_central_directory(tmp_path):
    data = patched(HONEST, central_record(HONEST, "extension/dist/extension.js") + 20, "<I", len(HONEST))
    assert "archive entry b'extension/dist/extension.js' runs into the central directory" in rejected(tmp_path, data)


def test_rejects_entries_that_overlap(tmp_path):
    # Two directory records for one local header: an entry hidden inside another's bytes.
    at = central_record(HONEST, "extension/README.md")
    n, m, k = struct.unpack_from("<HHH", HONEST, at + 28)
    central = CENTRAL + HONEST[at : at + 46 + n + m + k]
    data = LOCAL + central + end_record(COUNT + 1, len(central), len(LOCAL))
    assert "archive entries overlap at offset" in rejected(tmp_path, data)


def test_rejects_zipfile_listing_that_differs_from_the_checked_directory(tmp_path, monkeypatch):
    # Defence in depth: whatever zipfile reads must be the directory the structural checks passed.
    real = validator.check_structure
    monkeypatch.setattr(validator, "check_structure", lambda data: [(name, crc, csize, size, offset + 1) for name, crc, csize, size, offset in real(data)])
    assert "Python's zipfile lists different entries" in rejected(tmp_path)


# --- Malformed manifests ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "pkg, reason",
    [
        (b"not json", "not strict JSON"),
        (b"\xef\xbb\xbf" + package_json(), "not strict JSON"),  # a BOM: JSON.parse rejects it too
        (b'["ukaisi", "inspect-ai", "0.9.19"]', "not a JSON object"),
        (b'{"name": "inspect-ai", "publisher": "ukaisi", "version": "0.9.19", "publisher": "other"}', "duplicate keys"),
        (b'{"name": "inspect-ai", "publisher": "ukaisi"}', "version None is missing"),
        (b'{"name": "inspect-ai", "publisher": "ukaisi", "version": 1}', "version 1 is missing or malformed"),
        (package_json(version="0.9.19-rc.1"), "version '0.9.19-rc.1' is missing or malformed"),
        (package_json(version="v0.9.19"), "malformed"),
        (package_json(version="0.09.19"), "malformed"),
        (package_json(publisher="uk.aisi"), "publisher 'uk.aisi' is missing or malformed"),
        (package_json(name="inspect ai"), "name 'inspect ai' is missing or malformed"),
        (package_json(name=""), "malformed"),
        # Python's json accepts these; JSON.parse (vsce, ovsx) does not, so they must not reach a publisher
        (package_json().replace(b'"displayName": "Inspect AI"', b'"extra": NaN'), "NaN is not JSON"),
        (package_json().replace(b'"displayName": "Inspect AI"', b'"extra": Infinity'), "Infinity is not JSON"),
        (package_json().replace(b'"displayName": "Inspect AI"', b'"extra": -Infinity'), "-Infinity is not JSON"),
    ],
)
def test_rejects_malformed_package_json(tmp_path, pkg, reason):
    assert reason in rejected(tmp_path, entries(pkg=pkg))


ENTITY_MANIFEST = vsixmanifest().replace(b"<PackageManifest", b'<!DOCTYPE x [<!ENTITY p "ukaisi">]><PackageManifest', 1).replace(b'Publisher="ukaisi"', b'Publisher="&p;"')


@pytest.mark.parametrize(
    "manifest, reason",
    [
        (b"<not xml", "not well-formed XML"),
        (ENTITY_MANIFEST, "declares a DTD or entities"),
        (vsixmanifest().replace(b"<PackageManifest", b"<!DOCTYPE PackageManifest><PackageManifest", 1), "declares a DTD or entities"),
        (vsixmanifest().replace(VSX_NS.encode(), b"http://example.com/other", 1), "not PackageManifest"),
        (vsixmanifest().replace(b"<Metadata>", b"<Metadata><Identity Id=\"other-extension\" Version=\"0.9.19\" Publisher=\"ukaisi\"/>", 1), "expected one Identity element under Metadata"),
        (vsixmanifest().replace(b"</Metadata>", b"</Metadata><Metadata/>", 1), "expected one Metadata element under the root"),
        # vsce's xml2js reader ignores namespaces and takes the first Metadata / Identity it meets
        (FOREIGN_METADATA, "expected one Metadata element under the root, found ['{urn:other}Metadata'"),
        (FOREIGN_IDENTITY, "expected one Identity element under Metadata, found ['{urn:other}Identity'"),
        (vsixmanifest(installation_extra='<Identity Id="other-extension" Version="0.9.20" Publisher="ukaisi"/>'), "expected one Identity element under Metadata"),
        (vsixmanifest(installation_extra="<Metadata/>"), "expected one Metadata element under the root"),
        (vsixmanifest(before_metadata='<Installation xmlns="urn:other"><Metadata/></Installation>'), "expected one Metadata element under the root"),
        (vsixmanifest(identity_attrs='Id="inspect-ai" Version="0.9.19" Publisher="ukaisi" d:Id="other-extension"'), "Identity has namespaced attributes"),
        (vsixmanifest(identity_attrs='Id="inspect-ai" Version="0.9.19"'), "publisher None is missing"),
        (vsixmanifest(identity_attrs='Id="inspect-ai" Publisher="ukaisi"'), "version None is missing"),
        (vsixmanifest(version="0.9.19-rc.1"), "malformed"),
        (b"\xff\xfe" + vsixmanifest(), "not UTF-8"),
    ],
)
def test_rejects_malformed_vsixmanifest(tmp_path, manifest, reason):
    assert reason in rejected(tmp_path, entries(manifest=manifest))


# --- The artifact directory ----------------------------------------------------------------


def test_rejects_empty_directory(tmp_path):
    (tmp_path / "vsix").mkdir()
    with pytest.raises(Rejected, match="exactly one file"):
        validator.verify(str(tmp_path / "vsix"), EXTENSION_ID, TAG)


def test_rejects_second_file_however_named(tmp_path):
    write_vsix(tmp_path / "vsix")
    (tmp_path / "vsix" / ".hidden").write_text("")
    with pytest.raises(Rejected, match="exactly one file"):
        validator.verify(str(tmp_path / "vsix"), EXTENSION_ID, TAG)


def test_rejects_symlink_artifact(tmp_path):
    real = write_vsix(tmp_path / "elsewhere")
    (tmp_path / "vsix").mkdir()
    os.symlink(real, tmp_path / "vsix" / FILENAME)
    with pytest.raises(Rejected, match="not a regular file"):
        validator.verify(str(tmp_path / "vsix"), EXTENSION_ID, TAG)


def test_rejects_directory_artifact(tmp_path):
    write_vsix(tmp_path / "vsix" / FILENAME)
    with pytest.raises(Rejected, match="not a regular file"):
        validator.verify(str(tmp_path / "vsix"), EXTENSION_ID, TAG)


@pytest.mark.parametrize("filename", ["inspect-ai.vsix.sh", "inspect ai.vsix", ".vsix", "-o.vsix", "inspect-ai.VSIX", "inspect-ai.zip", "$(id).vsix"])
def test_rejects_artifact_names_that_are_not_plain(tmp_path, filename):
    assert "not a plain <name>.vsix" in rejected(tmp_path, filename=filename)


# --- The trusted inputs must have the expected shape too -------------------------------------


@pytest.mark.parametrize("extension_id", ["inspect-ai", "ukaisi/inspect-ai", "ukaisi.inspect-ai.extra", "uk aisi.inspect-ai", "", "ukaisi."])
def test_rejects_extension_id_that_is_not_publisher_dot_name(tmp_path, extension_id):
    assert "not <publisher>.<name>" in rejected(tmp_path, extension_id=extension_id)


@pytest.mark.parametrize("tag", ["v0.9", "v0.9.19-rc.1", "v0.9.19.1", "release", "", "v01.9.19", "0.9.19v", "v0.9.19 ", "inspect-ai_v0.9.19"])
def test_rejects_tag_without_a_trailing_semver(tmp_path, tag):
    assert "does not end in an X.Y.Z version" in rejected(tmp_path, tag=tag)


# --- The step as bash runs it ------------------------------------------------------------------


def run_bash(script: str, *, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    # GitHub runs `run:` scripts with `bash --noprofile --norc -eo pipefail`.
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], cwd=cwd, env={**os.environ, **env}, text=True, capture_output=True, check=False)


def read_outputs(path: Path) -> dict[str, str]:
    """Parse a GITHUB_OUTPUT file written with heredoc delimiters."""
    outputs: dict[str, str] = {}
    lines = path.read_text().splitlines()
    i = 0
    while i < len(lines):
        m = re.fullmatch(r"([\w-]+)<<(\S+)", lines[i])
        assert m, f"output not written with a heredoc delimiter: {lines[i]!r}"
        end = lines.index(m.group(2), i + 1)
        outputs[m.group(1)] = "\n".join(lines[i + 1 : end])
        i = end + 1
    return outputs


def test_step_writes_its_outputs_with_heredocs_and_uses_the_step_env(tmp_path):
    write_vsix(tmp_path / "vsix")
    output = tmp_path / "output"
    output.touch()
    s = step(VERIFY)
    assert s["env"] == {"VSIX_DIR": "vsix"}
    env = {**s["env"], "EXTENSION_ID": EXTENSION_ID, "TAG": TAG, "GITHUB_OUTPUT": str(output)}
    result = run_bash(s["run"], cwd=tmp_path, env=env)
    assert result.returncode == 0, result.stderr
    assert read_outputs(output) == {"vsix": f"vsix/{FILENAME}", "version": VERSION, "publisher": PUBLISHER, "name": NAME}
    assert f"Verified vsix/{FILENAME}: {EXTENSION_ID} {VERSION} (tag {TAG})" in result.stdout


def test_step_fails_with_an_error_annotation_and_no_outputs(tmp_path):
    write_vsix(tmp_path / "vsix", consistent(name="other-extension"))
    output = tmp_path / "output"
    output.touch()
    env = {**step(VERIFY)["env"], "EXTENSION_ID": EXTENSION_ID, "TAG": TAG, "GITHUB_OUTPUT": str(output)}
    result = run_bash(step(VERIFY)["run"], cwd=tmp_path, env=env)
    assert result.returncode == 1
    assert "::error::VSIX rejected: package is ukaisi.other-extension 0.9.19" in result.stdout
    assert output.read_text() == ""


# --- The publish job, step by step ------------------------------------------------------------

FAKE_TOOL = f'''#!{sys.executable}
"""Stand-in for npm, vsce, ovsx and gh: records every call; serves canned listings from $FAKE_STORE."""
import json, os, pathlib, sys

tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
store = pathlib.Path(os.environ["FAKE_STORE"])
with (store / "calls.jsonl").open("a") as log:
    log.write(json.dumps({{"tool": tool, "argv": args}}) + "\\n")
canned = store / f"{{tool}}-{{args[0] if args else ''}}.txt"
if canned.exists():
    sys.stdout.write(canned.read_text())
    sys.exit(0)
if tool == "ovsx" and args[:1] == ["get"]:
    sys.exit("Error: Extension not found: " + args[1])
'''

# Stand-in for vsce's out/zip.js, which the cross-check step finds next to the `vsce` on PATH:
# records the call and returns the canned view from $FAKE_STORE/vsce-read.json, or throws.
FAKE_READER = """
const fs = require("fs"), path = require("path");
exports.readVSIXPackage = async (packagePath) => {
  const store = process.env.FAKE_STORE;
  fs.appendFileSync(path.join(store, "calls.jsonl"), JSON.stringify({ tool: "readVSIXPackage", argv: [packagePath] }) + "\\n");
  const canned = path.join(store, "vsce-read.json");
  if (!fs.existsSync(canned)) throw new Error("Manifest not found");
  return JSON.parse(fs.readFileSync(canned, "utf8"));
};
"""


def vsce_view(publisher=PUBLISHER, name=NAME, version=VERSION, *, xml: dict | None = None) -> dict:
    """What readVSIXPackage returns: the parsed package.json and the xml2js form of the vsixmanifest."""
    identity = {"Language": "en-US", "Id": name, "Version": version, "Publisher": publisher} if xml is None else xml
    return {"manifest": {"publisher": publisher, "name": name, "version": version}, "xmlManifest": {"PackageManifest": {"Metadata": [{"Identity": [{"$": identity}]}]}}}


SECRETS = {"VSCE_PAT": "fake-vsce-pat", "OVSX_PAT": "fake-ovsx-pat", "GITHUB_TOKEN": "fake-github-token"}
EXPRESSIONS = {
    "${{ inputs.extension-id }}": EXTENSION_ID,
    "${{ inputs.vsce-version }}": "3.9.2",
    "${{ inputs.ovsx-version }}": "0.10.12",
    "${{ needs.release-please.outputs.tag_name }}": TAG,
    "${{ github.repository }}": "meridianlabs-ai/inspect_vscode",
    **{f"${{{{ secrets.{k} }}}}": v for k, v in SECRETS.items()},
}


@dataclass
class JobRun:
    outcomes: dict = field(default_factory=dict)  # by step name: success, failure, skipped
    logs: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def argv(self, tool: str) -> list[list[str]]:
        return [c["argv"] for c in self.calls if c["tool"] == tool]


def resolve(value, outputs: dict[str, str]) -> str:
    value = str(value)
    if m := re.fullmatch(r"\$\{\{ steps\.verify\.outputs\.(\w+) \}\}", value):
        return outputs[m.group(1)]
    if value.startswith("${{"):
        assert value in EXPRESSIONS, f"expression not modelled: {value}"
        return EXPRESSIONS[value]
    assert "${{" not in value, f"expression inside a value is not modelled: {value}"
    return value


def should_run(condition, job_ok: bool) -> bool:
    if condition is None:
        return job_ok
    if condition == "${{ inputs.publish-openvsx }}":
        return job_ok  # the default input is true
    raise AssertionError(f"step condition not modelled: {condition}")


def run_publish_job(tmp_path: Path, *, items=None, filename: str = FILENAME, vsce_show: str | None = None, ovsx_get: str | None = None, vsce_reads: dict | None = vsce_view()) -> JobRun:
    job = workflow()["jobs"]["publish"]
    store = tmp_path / "store"
    store.mkdir()
    if vsce_reads is not None:
        (store / "vsce-read.json").write_text(json.dumps(vsce_reads))
    if vsce_show is not None:
        (store / "vsce-show.txt").write_text(vsce_show)
    if ovsx_get is not None:
        (store / "ovsx-get.txt").write_text(ovsx_get)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("npm", "vsce", "ovsx", "gh"):
        (bin_dir / tool).write_text(FAKE_TOOL)
        (bin_dir / tool).chmod(0o755)
    (bin_dir / "out").mkdir()
    (bin_dir / "out" / "zip.js").write_text(FAKE_READER)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    job_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_STORE": str(store),
        "RUNNER_TEMP": str(runner_temp),
        **{k: resolve(v, {}) for k, v in job["env"].items()},
    }
    run = JobRun()
    outputs: dict[str, str] = {}
    job_ok = True
    for s in job["steps"]:
        name = s.get("name") or s["uses"]
        if not should_run(s.get("if"), job_ok):
            run.outcomes[name] = "skipped"
            continue
        if "uses" in s:
            if s["uses"].startswith("actions/download-artifact@"):
                assert s["with"] == {"name": "vsix", "path": "vsix"}
                write_vsix(workspace / "vsix", items, filename)
            else:
                assert s["uses"].startswith("actions/setup-node@"), f"uses step not modelled: {s['uses']}"
            run.outcomes[name] = "success"
            continue
        output = tmp_path / f"output-{len(run.outcomes)}"
        output.touch()
        env = {**job_env, **{k: resolve(v, outputs) for k, v in s.get("env", {}).items()}, "GITHUB_OUTPUT": str(output)}
        result = run_bash(s["run"], cwd=workspace, env=env)
        run.logs[name] = result
        if s.get("id") == "verify":
            outputs = read_outputs(output)
        run.outcomes[name] = "success" if result.returncode == 0 else "failure"
        job_ok = job_ok and result.returncode == 0
    calls = store / "calls.jsonl"
    run.calls = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    return run


def test_intended_release_is_verified_then_published_and_uploaded(tmp_path):
    run = run_publish_job(tmp_path)
    assert run.outcomes == {"Setup Node.js": "success", "Download VSIX artifact": "success", VERIFY: "success", INSTALL: "success", CROSSCHECK: "success", MARKETPLACE: "success", OPENVSX: "success", UPLOAD: "success"}
    vsix = f"vsix/{FILENAME}"
    assert run.argv("npm") == [["install", "-g", "--ignore-scripts", "@vscode/vsce@3.9.2", "ovsx@0.10.12"]]
    assert run.argv("readVSIXPackage") == [[vsix]]
    assert f"vsce reads {vsix} as {EXTENSION_ID} {VERSION}" in run.logs[CROSSCHECK].stdout
    assert run.argv("vsce") == [["show", EXTENSION_ID, "--json"], ["publish", "--packagePath", vsix, "--pat", "fake-vsce-pat"]]
    assert run.argv("ovsx") == [["get", EXTENSION_ID, "--metadata"], ["publish", vsix, "--pat", "fake-ovsx-pat"]]
    assert run.argv("gh") == [["release", "upload", TAG, vsix, "--clobber"]]


@pytest.mark.parametrize(
    "items",
    [
        consistent(name="other-extension"),
        consistent(publisher="other-publisher"),
        consistent(version="0.9.20"),
        entries(pkg=package_json(name="other-extension")),  # manifests disagree
        entries() + [("EXTENSION/PACKAGE.JSON", package_json(name="other-extension"))],
        ALIASED_ENTRIES,
        entries(manifest=FOREIGN_METADATA),
        entries(manifest=FOREIGN_IDENTITY),
        entries(pkg=package_json().replace(b'"displayName": "Inspect AI"', b'"extra": NaN')),
        DUAL_LOCATOR,
        DUAL_SENTINEL,
    ],
    ids=["other-name", "other-publisher", "other-version", "disagreeing-manifests", "case-variant-manifest", "unicode-path-aliases", "foreign-namespace-metadata", "foreign-namespace-identity", "nan-in-package-json", "two-directories-zip64-locator", "two-directories-zip64-sentinels"],
)
def test_hostile_package_fails_verify_and_no_publisher_or_installer_runs(tmp_path, items):
    run = run_publish_job(tmp_path, items=items)
    assert run.outcomes[VERIFY] == "failure"
    assert "::error::VSIX rejected" in run.logs[VERIFY].stdout
    assert {n: o for n, o in run.outcomes.items() if n in (INSTALL, CROSSCHECK, MARKETPLACE, OPENVSX, UPLOAD)} == {INSTALL: "skipped", CROSSCHECK: "skipped", MARKETPLACE: "skipped", OPENVSX: "skipped", UPLOAD: "skipped"}
    assert run.calls == []


@pytest.mark.parametrize(
    "vsce_reads, reason",
    [
        (vsce_view(name="other-extension"), 'vsce reads extension/package.json as ["ukaisi","other-extension","0.9.19"], but Verify VSIX read ["ukaisi","inspect-ai","0.9.19"]'),
        (vsce_view(publisher="other-publisher"), "vsce reads extension/package.json as"),
        (vsce_view(version="0.9.20"), "vsce reads extension/package.json as"),
        (vsce_view(version="0.9.19 "), "vsce reads extension/package.json as"),
        (vsce_view(xml={"Id": "other-extension", "Version": VERSION, "Publisher": PUBLISHER}), 'vsce reads extension.vsixmanifest as ["ukaisi","other-extension","0.9.19"]'),
        (vsce_view(xml={"Id": NAME, "Version": VERSION}), "vsce reads extension.vsixmanifest as"),
        ({"manifest": {"publisher": PUBLISHER, "name": NAME, "version": VERSION}, "xmlManifest": {"PackageManifest": {}}}, "vsce's reader failed on vsix/"),
        (None, "vsce's reader failed on vsix/inspect-ai-0.9.19.vsix: Manifest not found"),
    ],
    ids=["other-name", "other-publisher", "other-version", "version-not-exact", "xml-other-id", "xml-no-publisher", "xml-no-identity", "reader-throws"],
)
def test_vsce_reader_disagreeing_with_verify_fails_the_job_before_any_pat(tmp_path, vsce_reads, reason):
    run = run_publish_job(tmp_path, vsce_reads=vsce_reads)
    assert run.outcomes[VERIFY] == "success" and run.outcomes[CROSSCHECK] == "failure"
    assert f"::error::VSIX rejected: {reason}" in run.logs[CROSSCHECK].stdout
    assert {n: run.outcomes[n] for n in (MARKETPLACE, OPENVSX, UPLOAD)} == {MARKETPLACE: "skipped", OPENVSX: "skipped", UPLOAD: "skipped"}
    assert [c["tool"] for c in run.calls] == ["npm", "readVSIXPackage"]  # installed and read; nothing published


def test_vsce_reader_that_never_settles_fails_the_job(tmp_path):
    run_publish_job(tmp_path)  # lay out the stand-ins, then make the reader hang
    (tmp_path / "bin" / "out" / "zip.js").write_text("exports.readVSIXPackage = () => new Promise(() => {});\n")
    s = step(CROSSCHECK)
    env = {"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", **{k: resolve(v, {"vsix": f"vsix/{FILENAME}", "version": VERSION, "publisher": PUBLISHER, "name": NAME}) for k, v in s["env"].items()}}
    result = run_bash(s["run"], cwd=tmp_path / "workspace", env=env)
    assert result.returncode == 1
    assert f"::error::VSIX rejected: vsce's reader did not finish reading vsix/{FILENAME}" in result.stdout


def test_second_artifact_file_fails_verify(tmp_path):
    run = run_publish_job(tmp_path, items=[("extension.vsixmanifest", vsixmanifest())], filename="extra.vsix")
    assert run.outcomes[VERIFY] == "failure" and run.calls == []


def test_verify_runs_after_download_and_before_any_step_that_holds_a_secret_or_installs():
    names = [s.get("name") or s["uses"] for s in publish_steps()]
    verify = names.index(VERIFY)
    assert names.index("Download VSIX artifact") < verify < names.index(INSTALL) < names.index(CROSSCHECK) < names.index(MARKETPLACE)
    for s in publish_steps()[: verify + 1]:
        assert "secrets." not in yaml.safe_dump(s), f"{s.get('name')} holds a secret before the package is verified"
    for s in publish_steps():
        if "secrets." in yaml.safe_dump(s.get("env", {})):
            assert s["name"] in (MARKETPLACE, OPENVSX, UPLOAD)


def test_publish_job_uses_nothing_the_build_job_produced_except_the_artifact():
    job = workflow()["jobs"]["publish"]
    assert "needs.build" not in yaml.safe_dump(job)
    assert "version" not in workflow()["jobs"]["build"]["outputs"]
    for name in (CROSSCHECK, MARKETPLACE, OPENVSX, UPLOAD):
        assert step(name)["env"]["VSIX"] == "${{ steps.verify.outputs.vsix }}"
    for name in (CROSSCHECK, MARKETPLACE, OPENVSX):
        assert step(name)["env"]["VERSION"] == "${{ steps.verify.outputs.version }}"
        assert step(name)["env"]["PUBLISHER"] == "${{ steps.verify.outputs.publisher }}"
        assert step(name)["env"]["NAME"] == "${{ steps.verify.outputs.name }}"
    assert job["env"]["EXTENSION_ID"] == "${{ inputs.extension-id }}"
    assert job["env"]["TAG"] == "${{ needs.release-please.outputs.tag_name }}"
    assert not any(s.get("uses", "").startswith("actions/checkout") for s in job["steps"])


def vsce_listing(*versions: str, publisher: str | None = PUBLISHER, name: str | None = NAME) -> str:
    listing = {"publisher": {"publisherName": publisher, "displayName": "UK AISI"}, "extensionName": name, "versions": [{"version": v, "flags": "validated"} for v in versions]}
    if publisher is None:
        del listing["publisher"]
    if name is None:
        del listing["extensionName"]
    return json.dumps(listing, indent="\t")


def ovsx_listing(latest: str, *others: str, namespace: str | None = PUBLISHER, name: str | None = NAME) -> str:
    listing = {"namespace": namespace, "name": name, "version": latest, "allVersions": {v: f"https://open-vsx.org/api/{PUBLISHER}/{NAME}/{v}" for v in (latest, *others)}}
    return json.dumps({k: v for k, v in listing.items() if v is not None}, indent=4)


@pytest.mark.parametrize(
    "vsce_show, published",
    [
        (vsce_listing("0.9.19", "0.9.18"), False),  # exact version present: skip
        (vsce_listing("0.9.190", "0.9.18"), True),  # a longer version sharing the prefix is not it
        (vsce_listing("0.9.1"), True),
        (vsce_listing(), True),
        ("undefined\n", True),  # what `vsce show --json` prints for an unknown extension
        ('{"versions": "0.9.19"}\n', True),  # not the documented shape
        ("", True),
        # a listing for anything but the verified identity never skips
        (vsce_listing("0.9.19", publisher="other-publisher"), True),
        (vsce_listing("0.9.19", name="other-extension"), True),
        (vsce_listing("0.9.19", publisher=None), True),
        (vsce_listing("0.9.19", name=None), True),
        (vsce_listing("0.9.19", publisher=PUBLISHER.upper()), True),
    ],
)
def test_marketplace_skip_check_compares_the_exact_identity_and_version_from_the_listing(tmp_path, vsce_show, published):
    run = run_publish_job(tmp_path, vsce_show=vsce_show)
    assert run.outcomes[MARKETPLACE] == "success"
    assert (["publish", "--packagePath", f"vsix/{FILENAME}", "--pat", "fake-vsce-pat"] in run.argv("vsce")) is published
    assert ("already on VS Code Marketplace" in run.logs[MARKETPLACE].stdout) is not published


@pytest.mark.parametrize(
    "ovsx_get, published",
    [
        (ovsx_listing("0.9.19", "0.9.18"), False),  # latest is the version: skip
        (ovsx_listing("0.9.20", "0.9.19"), False),  # an older published version: skip
        (ovsx_listing("0.9.190", "0.9.18"), True),
        (ovsx_listing("0.9.20", "0.9.190"), True),
        (None, True),  # `ovsx get` fails for an unknown extension
        ("not json\n", True),
        (ovsx_listing("0.9.19", namespace="other-publisher"), True),
        (ovsx_listing("0.9.19", name="other-extension"), True),
        (ovsx_listing("0.9.19", namespace=None), True),
        (ovsx_listing("0.9.19", name=None), True),
    ],
)
def test_openvsx_skip_check_compares_the_exact_identity_and_version_from_the_metadata(tmp_path, ovsx_get, published):
    run = run_publish_job(tmp_path, ovsx_get=ovsx_get)
    assert run.outcomes[OPENVSX] == "success"
    assert (["publish", f"vsix/{FILENAME}", "--pat", "fake-ovsx-pat"] in run.argv("ovsx")) is published
    assert ("already on Open VSX" in run.logs[OPENVSX].stdout) is not published


# --- The cross-check against the real vsce --------------------------------------------------------

BOUND = {"vsix": f"vsix/{FILENAME}", "version": VERSION, "publisher": PUBLISHER, "name": NAME}


def pinned_vsce_version() -> str:
    doc = workflow()
    return doc.get("on", doc.get(True))["workflow_call"]["inputs"]["vsce-version"]["default"]  # PyYAML reads `on` as True


@pytest.fixture
def vsce_bin() -> Path:
    """The directory of the real `vsce` on PATH, when it is the workflow's default vsce-version."""
    pinned = pinned_vsce_version()
    found = shutil.which("vsce")
    manifest = Path(os.path.realpath(found)).parent / "package.json" if found else None
    version = json.loads(manifest.read_text()).get("version") if manifest and manifest.exists() else None
    if version != pinned:
        reason = f"needs vsce {pinned} on PATH; found {found or 'none'} (version {version})"
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(f"{reason}; tests.yml installs it")
        pytest.skip(reason)
    return Path(found).parent


def run_crosscheck(workspace: Path, vsce_bin: Path, outputs: dict[str, str]) -> subprocess.CompletedProcess:
    s = step(CROSSCHECK)
    env = {"PATH": f"{vsce_bin}:{os.environ['PATH']}", **{k: resolve(v, outputs) for k, v in s["env"].items()}}
    return run_bash(s["run"], cwd=workspace, env=env)


def test_tests_workflow_installs_the_vsce_the_publish_job_pins():
    steps = yaml.safe_load((ROOT / ".github" / "workflows" / "tests.yml").read_text())["jobs"]["pytest"]["steps"]
    assert f"npm install -g --ignore-scripts @vscode/vsce@{pinned_vsce_version()}" in [s.get("run") for s in steps]


@pytest.mark.parametrize("data", [DUAL_LOCATOR, DUAL_SENTINEL], ids=["zip64-locator-without-sentinels", "zip64-sentinels"])
def test_real_vsce_reads_the_other_extension_from_the_two_directory_layouts(tmp_path, vsce_bin, data):
    # The fixtures are the finding: vsce's reader takes the other directory. Were Verify VSIX to pass such
    # a package, the cross-check would still stop it.
    write_vsix(tmp_path / "vsix", data)
    result = run_crosscheck(tmp_path, vsce_bin, BOUND)
    assert result.returncode == 1
    assert '::error::VSIX rejected: vsce reads extension/package.json as ["ukaisi","other-extension","0.9.19"], but Verify VSIX read ["ukaisi","inspect-ai","0.9.19"]' in result.stdout


def test_extension_packaged_by_real_vsce_passes_verify_and_the_cross_check(tmp_path, vsce_bin):
    ext = tmp_path / "ext"
    (ext / "dist").mkdir(parents=True)
    manifest = json.loads(package_json())
    manifest.update(main="./dist/extension.js", activationEvents=["onStartupFinished"], license="MIT", repository={"type": "git", "url": "https://github.com/meridianlabs-ai/inspect_vscode"})
    (ext / "package.json").write_text(json.dumps(manifest, indent=2))
    (ext / "dist" / "extension.js").write_text("module.exports = {};\n")
    (ext / "README.md").write_text("# Inspect AI\n")
    (ext / "LICENSE").write_text("MIT\n")
    (tmp_path / "vsix").mkdir()
    env = {**os.environ, "PATH": f"{vsce_bin}:{os.environ['PATH']}"}
    packaged = subprocess.run(["vsce", "package", "--no-dependencies", "-o", str(tmp_path / "vsix" / FILENAME)], cwd=ext, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
    assert packaged.returncode == 0, packaged.stdout + packaged.stderr
    # vsce streams package.json from disk: bit 3 set, zeros in its local header, sizes in a data descriptor
    assert zipfile.ZipFile(tmp_path / "vsix" / FILENAME).getinfo("extension/package.json").flag_bits & 8
    output = tmp_path / "output"
    output.touch()
    verify = run_bash(step(VERIFY)["run"], cwd=tmp_path, env={**step(VERIFY)["env"], "EXTENSION_ID": EXTENSION_ID, "TAG": TAG, "GITHUB_OUTPUT": str(output)})
    assert verify.returncode == 0, verify.stdout
    assert read_outputs(output) == BOUND
    result = run_crosscheck(tmp_path, vsce_bin, BOUND)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"vsce reads vsix/{FILENAME} as {EXTENSION_ID} {VERSION}" in result.stdout
