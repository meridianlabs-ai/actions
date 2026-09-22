"""Tests for the publish job of release-please-vscode.yml: what binds the .vsix to the release.

The build job runs the consuming repo's code (install hooks, scripts, tests,
vsce:package), so everything it produces, the .vsix, its file name and its
version output, is repo-controlled. vsce and ovsx publish to whatever
publisher, name and version the package's own manifests declare, with PATs
that reach every extension the account manages. The publish job's Verify VSIX
step is therefore the control: before any step holds a PAT it must open the
downloaded package without executing anything in it and fail unless
extension/package.json and extension.vsixmanifest agree and name exactly the
caller's `extension-id` at the version in the release-please tag.

The validator is inline in the workflow (a Python heredoc), so the tests lift
it from the YAML and exercise the real code: in-process against fixture
packages built here (intended releases in each tag shape the callers'
release-please configs produce, swapped identities, tag mismatches,
disagreeing manifests, ambiguous or unsafe archives, malformed inputs), and
end to end by running the publish job's `run:` steps in order under bash with
stand-ins for npm, vsce, ovsx and gh on PATH that record what they were asked
to do. No package is published and no real PAT exists here.

Run with `python3 -m pytest` from the repo root (needs pytest and PyYAML;
`.github/workflows/tests.yml` does the same in CI). jq must be on PATH, as it
is on the hosted runners.
"""

from __future__ import annotations

import json
import os
import re
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


def write_vsix(directory: Path, items: list[tuple[str | zipfile.ZipInfo, bytes]] | None = None, filename: str = FILENAME) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns about duplicate names; some tests write them on purpose
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in entries() if items is None else items:
                zf.writestr(name, data)
    return path


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
    with pytest.raises(Rejected, match="not a readable zip archive"):
        validator.verify(str(tmp_path / "vsix"), EXTENSION_ID, TAG)


def test_rejects_oversized_manifest(tmp_path):
    big = package_json(padding="x" * (1 << 20))
    assert "larger than" in rejected(tmp_path, entries(pkg=big))


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


def run_publish_job(tmp_path: Path, *, items=None, filename: str = FILENAME, vsce_show: str | None = None, ovsx_get: str | None = None) -> JobRun:
    job = workflow()["jobs"]["publish"]
    store = tmp_path / "store"
    store.mkdir()
    if vsce_show is not None:
        (store / "vsce-show.txt").write_text(vsce_show)
    if ovsx_get is not None:
        (store / "ovsx-get.txt").write_text(ovsx_get)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("npm", "vsce", "ovsx", "gh"):
        (bin_dir / tool).write_text(FAKE_TOOL)
        (bin_dir / tool).chmod(0o755)
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
    assert run.outcomes == {"Setup Node.js": "success", "Download VSIX artifact": "success", VERIFY: "success", INSTALL: "success", MARKETPLACE: "success", OPENVSX: "success", UPLOAD: "success"}
    vsix = f"vsix/{FILENAME}"
    assert run.argv("npm") == [["install", "-g", "--ignore-scripts", "@vscode/vsce@3.9.2", "ovsx@0.10.12"]]
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
    ],
    ids=["other-name", "other-publisher", "other-version", "disagreeing-manifests", "case-variant-manifest", "unicode-path-aliases", "foreign-namespace-metadata", "foreign-namespace-identity", "nan-in-package-json"],
)
def test_hostile_package_fails_verify_and_no_publisher_or_installer_runs(tmp_path, items):
    run = run_publish_job(tmp_path, items=items)
    assert run.outcomes[VERIFY] == "failure"
    assert "::error::VSIX rejected" in run.logs[VERIFY].stdout
    assert {n: o for n, o in run.outcomes.items() if n in (INSTALL, MARKETPLACE, OPENVSX, UPLOAD)} == {INSTALL: "skipped", MARKETPLACE: "skipped", OPENVSX: "skipped", UPLOAD: "skipped"}
    assert run.calls == []


def test_second_artifact_file_fails_verify(tmp_path):
    run = run_publish_job(tmp_path, items=[("extension.vsixmanifest", vsixmanifest())], filename="extra.vsix")
    assert run.outcomes[VERIFY] == "failure" and run.calls == []


def test_verify_runs_after_download_and_before_any_step_that_holds_a_secret_or_installs():
    names = [s.get("name") or s["uses"] for s in publish_steps()]
    verify = names.index(VERIFY)
    assert names.index("Download VSIX artifact") < verify < names.index(INSTALL)
    for s in publish_steps()[: verify + 1]:
        assert "secrets." not in yaml.safe_dump(s), f"{s.get('name')} holds a secret before the package is verified"
    for s in publish_steps():
        if "secrets." in yaml.safe_dump(s.get("env", {})):
            assert s["name"] in (MARKETPLACE, OPENVSX, UPLOAD)


def test_publish_job_uses_nothing_the_build_job_produced_except_the_artifact():
    job = workflow()["jobs"]["publish"]
    assert "needs.build" not in yaml.safe_dump(job)
    assert "version" not in workflow()["jobs"]["build"]["outputs"]
    for name in (MARKETPLACE, OPENVSX, UPLOAD):
        assert step(name)["env"]["VSIX"] == "${{ steps.verify.outputs.vsix }}"
    for name in (MARKETPLACE, OPENVSX):
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
