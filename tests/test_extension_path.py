"""The companion folder resolves in a checkout and in an installed wheel."""

from __future__ import annotations

import json

from web_search_neo import extension_path


def _extension(folder, version):
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (folder / "service-worker.js").write_text("// worker", encoding="utf-8")
    (folder / "bridge-token.js").write_text("export const TOKEN = 'secret';", encoding="utf-8")


def test_a_checkout_uses_its_own_folder():
    assert extension_path.resolve_extension_dir() == extension_path.CHECKOUT_DIR.resolve()


def test_an_installed_copy_is_mirrored_per_user_without_the_secret(tmp_path, monkeypatch):
    packaged = tmp_path / "site" / "chrome_extension"
    _extension(packaged, "9.9.9")
    monkeypatch.setattr(extension_path, "CHECKOUT_DIR", tmp_path / "missing")
    monkeypatch.setattr(extension_path, "PACKAGED_DIR", packaged)
    monkeypatch.setattr(extension_path, "_user_data_root", lambda: tmp_path / "user")

    resolved = extension_path.resolve_extension_dir()

    assert resolved == (tmp_path / "user" / "extension").resolve()
    assert (resolved / "service-worker.js").is_file()
    # The machine-local secret is written there later, never copied from a wheel.
    assert not (resolved / "bridge-token.js").exists()


def test_a_current_mirror_is_left_alone(tmp_path, monkeypatch):
    packaged = tmp_path / "site" / "chrome_extension"
    _extension(packaged, "1.0.0")
    target = tmp_path / "user" / "extension"
    extension_path._mirror(packaged, target)
    (target / "bridge-token.js").write_text("local", encoding="utf-8")
    # Same name and size: the completion marker still matches, nothing is copied.
    (packaged / "service-worker.js").write_text("// WORKER", encoding="utf-8")

    extension_path._mirror(packaged, target)

    assert (target / "service-worker.js").read_text(encoding="utf-8") == "// worker"
    assert (target / "bridge-token.js").read_text(encoding="utf-8") == "local"


def test_a_half_copied_mirror_with_the_right_version_is_redone(tmp_path):
    packaged = tmp_path / "site" / "chrome_extension"
    _extension(packaged, "2.0.0")
    (packaged / "popup.js").write_text("// popup", encoding="utf-8")
    target = tmp_path / "user" / "extension"
    # An interrupted copy of an older release: manifest already current, no marker.
    target.mkdir(parents=True)
    (target / "manifest.json").write_text(json.dumps({"version": "2.0.0"}), encoding="utf-8")
    (target / "bridge-token.js").write_text("local", encoding="utf-8")

    extension_path._mirror(packaged, target)

    assert (target / "popup.js").read_text(encoding="utf-8") == "// popup"
    assert (target / "service-worker.js").is_file()
    assert (target / extension_path.MIRROR_MARKER).is_file()
    # The machine-local secret is carried across the re-mirror.
    assert (target / "bridge-token.js").read_text(encoding="utf-8") == "local"
    assert [p.name for p in target.parent.iterdir()] == ["extension"]


def test_a_changed_package_replaces_the_mirror_and_keeps_the_secret(tmp_path):
    packaged = tmp_path / "site" / "chrome_extension"
    _extension(packaged, "1.0.0")
    target = tmp_path / "user" / "extension"
    extension_path._mirror(packaged, target)
    (target / "bridge-token.js").write_text("local", encoding="utf-8")
    (target / "stale.js").write_text("old", encoding="utf-8")
    (packaged / "service-worker.js").write_text("// a longer worker", encoding="utf-8")

    extension_path._mirror(packaged, target)

    assert (target / "service-worker.js").read_text(encoding="utf-8") == "// a longer worker"
    assert not (target / "stale.js").exists()
    assert (target / "bridge-token.js").read_text(encoding="utf-8") == "local"


def test_an_interrupted_copy_leaves_no_partial_mirror(tmp_path, monkeypatch):
    packaged = tmp_path / "site" / "chrome_extension"
    _extension(packaged, "1.0.0")
    target = tmp_path / "user" / "extension"

    def broken_copy(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(extension_path.shutil, "copy2", broken_copy)
    try:
        extension_path._mirror(packaged, target)
    except OSError:
        pass
    else:  # pragma: no cover
        raise AssertionError("the copy failure must propagate")
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_a_failed_mirror_never_falls_back_to_the_shared_packaged_dir(tmp_path, monkeypatch):
    packaged = tmp_path / "site" / "chrome_extension"
    _extension(packaged, "9.9.9")
    monkeypatch.setattr(extension_path, "CHECKOUT_DIR", tmp_path / "missing")
    monkeypatch.setattr(extension_path, "PACKAGED_DIR", packaged)
    monkeypatch.setattr(extension_path, "_user_data_root", lambda: tmp_path / "user")

    def refuse(*_args, **_kwargs):
        raise PermissionError("read-only profile")

    monkeypatch.setattr(extension_path, "_mirror", refuse)

    resolved = extension_path.resolve_extension_dir()

    assert resolved == tmp_path / "user" / "extension"
    assert resolved != packaged
