import os

import pytest

from core.path_security import PathSecurityError, ScopedPathResolver


def test_resolver_accepts_existing_files_directories_and_safe_new_targets(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    existing = root / "source.txt"
    existing.write_text("data", encoding="utf-8")
    resolver = ScopedPathResolver([root])

    assert resolver.existing_file(str(existing)) == existing.resolve()
    assert resolver.directory(str(root)) == root.resolve()
    assert resolver.new_file(str(root / "target.txt")) == root.resolve() / "target.txt"


def test_resolver_supports_multiple_roots(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    target = second / "file.txt"
    target.write_text("ok", encoding="utf-8")

    resolver = ScopedPathResolver([first, second])

    assert resolver.existing_file(str(target)) == target.resolve()


@pytest.mark.parametrize(
    "value",
    [
        "relative.txt",
        "%TEMP%\\file.txt",
        "$env:TEMP\\file.txt",
        "~/file.txt",
        "C:\\safe\\*.txt",
    ],
)
def test_resolver_rejects_relative_expansion_and_wildcard_inputs(tmp_path, value):
    root = tmp_path / "allowed"
    root.mkdir()
    resolver = ScopedPathResolver([root])

    with pytest.raises(PathSecurityError):
        resolver.new_file(value)


def test_resolver_rejects_sibling_prefix_and_parent_traversal(tmp_path):
    root = tmp_path / "allowed"
    sibling = tmp_path / "allowed-evil"
    root.mkdir()
    sibling.mkdir()
    resolver = ScopedPathResolver([root])

    with pytest.raises(PathSecurityError):
        resolver.new_file(str(sibling / "target.txt"))
    with pytest.raises(PathSecurityError):
        resolver.new_file(str(root / ".." / "escape.txt"))


def test_resolver_rejects_symlink_escape(tmp_path):
    root = tmp_path / "allowed"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink unavailable: {error}")
    resolver = ScopedPathResolver([root])

    with pytest.raises(PathSecurityError):
        resolver.new_file(str(link / "escape.txt"))


def test_resolver_rejects_missing_parent_existing_target_and_wrong_kind(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    existing = root / "exists.txt"
    existing.write_text("ok", encoding="utf-8")
    resolver = ScopedPathResolver([root])

    with pytest.raises(PathSecurityError, match="父目录"):
        resolver.new_file(str(root / "missing" / "target.txt"))
    with pytest.raises(PathSecurityError, match="已存在"):
        resolver.new_file(str(existing))
    with pytest.raises(PathSecurityError, match="普通文件"):
        resolver.existing_file(str(root))
    with pytest.raises(PathSecurityError, match="目录"):
        resolver.directory(str(existing))


def test_resolver_validates_allowed_roots_and_does_not_leak_input(tmp_path):
    with pytest.raises(PathSecurityError):
        ScopedPathResolver([])
    with pytest.raises(PathSecurityError):
        ScopedPathResolver([tmp_path / "missing"])

    root = tmp_path / "allowed"
    root.mkdir()
    resolver = ScopedPathResolver([root])
    secret = str(tmp_path / "private-secret.txt")
    with pytest.raises(PathSecurityError) as captured:
        resolver.new_file(secret)
    assert "private-secret" not in str(captured.value)
