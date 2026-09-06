"""Tests for SecurityFilter — Phase 3 defense-in-depth security layer."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from handoff_agent.security import (
    EXCLUDED_DIRECTORIES,
    EXCLUDED_EXTENSIONS,
    SENSITIVE_FILENAME_GLOBS,
    SENSITIVE_FILENAMES,
    FilterResult,
    SecurityFilter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sf(root: Path, **kwargs) -> SecurityFilter:
    """Create a SecurityFilter rooted at *root*."""
    return SecurityFilter(project_root=root, **kwargs)


def _touch(root: Path, rel: str, content: bytes | str = b"") -> Path:
    """Create a file at root/rel, creating parent dirs as needed."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        p.write_text(content)
    else:
        p.write_bytes(content)
    return p


# ===================================================================
# 1. Path Traversal Protection
# ===================================================================

class TestPathTraversal:
    """Ensure no file outside project_root can be accessed."""

    def test_absolute_path_outside_root(self, tmp_path: Path) -> None:
        outside = Path("/tmp/_handoff_test_outside")
        outside.mkdir(parents=True, exist_ok=True)
        (outside / ".env").write_text("SECRET=abc")
        try:
            sf = _sf(tmp_path)
            result = sf.is_excluded(str(outside / ".env"))
            assert result.excluded
            assert "traversal" in result.reason.lower()
        finally:
            (outside / ".env").unlink(missing_ok=True)
            outside.rmdir()

    def test_relative_traversal_dotdot(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/app.py", "print('hi')")
        sf = _sf(tmp_path)
        result = sf.is_excluded("../../../etc/passwd")
        assert result.excluded
        assert "traversal" in result.reason.lower()

    def test_nested_traversal(self, tmp_path: Path) -> None:
        sf = _sf(tmp_path)
        result = sf.is_excluded("src/../../.env")
        assert result.excluded

    def test_symlink_escaping_root(self, tmp_path: Path) -> None:
        """A symlink pointing outside root must be rejected."""
        outside = Path("/tmp/_handoff_test_symlink_target")
        outside.mkdir(parents=True, exist_ok=True)
        (outside / ".env").write_text("SECRET=abc")
        link = tmp_path / "link_to_env"
        try:
            link.symlink_to(outside / ".env")
        except OSError:
            pytest.skip("symlinks not supported in this environment")
        sf = _sf(tmp_path)
        result = sf.is_excluded("link_to_env")
        assert result.excluded
        assert "traversal" in result.reason.lower()
        link.unlink(missing_ok=True)
        (outside / ".env").unlink(missing_ok=True)
        outside.rmdir()

    def test_symlink_inside_root(self, tmp_path: Path) -> None:
        """A symlink that stays inside root should not be treated as traversal."""
        real = tmp_path / "real" / "app.py"
        _touch(tmp_path, "real/app.py", "print('ok')")
        link = tmp_path / "link.py"
        try:
            link.symlink_to(real)
        except OSError:
            pytest.skip("symlinks not supported in this environment")
        sf = _sf(tmp_path)
        result = sf.is_excluded("link.py")
        assert not result.excluded

    def test_valid_relative_path(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/main.py", "print('hello')")
        sf = _sf(tmp_path)
        result = sf.is_excluded("src/main.py")
        assert not result.excluded

    def test_valid_absolute_path_inside_root(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/main.py", "print('hello')")
        sf = _sf(tmp_path)
        result = sf.is_excluded(str(tmp_path / "src" / "main.py"))
        assert not result.excluded

    def test_path_with_double_slashes(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/app.py", "code")
        sf = _sf(tmp_path)
        result = sf.is_excluded("src//app.py")
        assert not result.excluded


# ===================================================================
# 2. Path / Filename Exclusion (Layer 1)
# ===================================================================

class TestSensitiveFilenameExclusion:
    """Exact sensitive filenames must be excluded without reading content."""

    @pytest.mark.parametrize("name", [
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".env.staging",
        ".env.test",
        ".env.backup",
        "id_rsa",
        "id_ed25519",
        "id_dsa",
        "id_ecdsa",
        "credentials",
        "credentials.json",
        "secrets",
        "secret.json",
        "secret.yaml",
        "secret.yml",
        "secret.toml",
        "token.json",
        "token.yaml",
        "token.yml",
        ".netrc",
        ".htpasswd",
        "keystore.jks",
        "keystore.properties",
    ])
    def test_exact_sensitive_filenames(self, tmp_path: Path, name: str) -> None:
        _touch(tmp_path, f"src/{name}", "some content")
        sf = _sf(tmp_path)
        result = sf.is_excluded(f"src/{name}")
        assert result.excluded
        assert "sensitive" in result.reason.lower()

    @pytest.mark.parametrize("pattern", [
        ".env.local",
        ".env.production",
        ".env.staging",
        ".env.development",
        ".env.test",
        ".env.backup",
    ])
    def test_env_variants_at_root(self, tmp_path: Path, pattern: str) -> None:
        _touch(tmp_path, pattern, "SECRET=x")
        sf = _sf(tmp_path)
        result = sf.is_excluded(pattern)
        assert result.excluded

    def test_env_in_subdirectory(self, tmp_path: Path) -> None:
        _touch(tmp_path, "config/.env", "API_KEY=abc")
        sf = _sf(tmp_path)
        result = sf.is_excluded("config/.env")
        assert result.excluded


class TestSensitiveFilenameGlobs:
    """Glob patterns for sensitive filenames."""

    @pytest.mark.parametrize("pattern", [
        "server.pem",
        "ca.pem",
        "private.key",
        "server.key",
        "cert.p12",
        "store.pfx",
        "trust.jks",
        "app.keystore",
        "leaf.cert",
        "ca.crt",
        "secret.yaml",
        "token.json",
        "password.txt",
        "credential.conf",
    ])
    def test_glob_sensitive_filenames(self, tmp_path: Path, pattern: str) -> None:
        _touch(tmp_path, f"config/{pattern}", "x")
        sf = _sf(tmp_path)
        result = sf.is_excluded(f"config/{pattern}")
        assert result.excluded
        assert "sensitive" in result.reason.lower()

    def test_pem_in_nested_dir(self, tmp_path: Path) -> None:
        _touch(tmp_path, "certs/keys/server.pem", "data")
        sf = _sf(tmp_path)
        result = sf.is_excluded("certs/keys/server.pem")
        assert result.excluded

    def test_password_prefix(self, tmp_path: Path) -> None:
        _touch(tmp_path, "config/passwords.txt", "list")
        sf = _sf(tmp_path)
        result = sf.is_excluded("config/passwords.txt")
        assert result.excluded


# ===================================================================
# 3. Directory Exclusion (Layer 2)
# ===================================================================

class TestDirectoryExclusion:
    """Excluded directories must be caught regardless of file inside them."""

    @pytest.mark.parametrize("dirname", [
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".eggs",
        ".cache",
        ".parcel-cache",
        ".next",
        ".nuxt",
        "coverage",
        ".nyc_output",
    ])
    def test_excluded_directories(self, tmp_path: Path, dirname: str) -> None:
        _touch(tmp_path, f"{dirname}/file.py", "code")
        sf = _sf(tmp_path)
        result = sf.is_excluded(f"{dirname}/file.py")
        assert result.excluded
        assert "directory" in result.reason.lower()

    def test_deeply_nested_excluded_dir(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/components/node_modules/lib/index.js", "x")
        sf = _sf(tmp_path)
        result = sf.is_excluded("src/components/node_modules/lib/index.js")
        assert result.excluded

    def test_pycache_in_nested_path(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/pkg/__pycache__/module.cpython-314.pyc", "x")
        sf = _sf(tmp_path)
        result = sf.is_excluded("src/pkg/__pycache__/module.cpython-314.pyc")
        assert result.excluded

    def test_venv_in_home(self, tmp_path: Path) -> None:
        _touch(tmp_path, ".venv/lib/python3/site.py", "x")
        sf = _sf(tmp_path)
        result = sf.is_excluded(".venv/lib/python3/site.py")
        assert result.excluded


# ===================================================================
# 4. Extension Exclusion (Layer 3)
# ===================================================================

class TestExtensionExclusion:
    """Binary / non-text extensions must be excluded."""

    @pytest.mark.parametrize("ext", [
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
        ".mp4", ".mp3", ".wav", ".avi", ".mov", ".mkv",
        ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
        ".class", ".o", ".so", ".dll", ".dylib", ".exe", ".bin",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx",
        ".woff", ".woff2", ".ttf", ".eot",
        ".pyc", ".pyo", ".whl",
    ])
    def test_excluded_extensions(self, tmp_path: Path, ext: str) -> None:
        _touch(tmp_path, f"assets/file{ext}", b"\x00\x00\x00\x00")
        sf = _sf(tmp_path)
        result = sf.is_excluded(f"assets/file{ext}")
        assert result.excluded
        assert "extension" in result.reason.lower()

    def test_svg_excluded(self, tmp_path: Path) -> None:
        _touch(tmp_path, "img/logo.svg", "<svg></svg>")
        sf = _sf(tmp_path)
        result = sf.is_excluded("img/logo.svg")
        assert result.excluded

    def test_jks_excluded(self, tmp_path: Path) -> None:
        _touch(tmp_path, "certs/trust.jks", b"\x00\x00")
        sf = _sf(tmp_path)
        result = sf.is_excluded("certs/trust.jks")
        assert result.excluded


# ===================================================================
# 5. Safe Files — Must NOT be excluded
# ===================================================================

class TestSafeFiles:
    """Common safe file types must pass through."""

    @pytest.mark.parametrize("name", [
        "main.py",
        "app.ts",
        "component.tsx",
        "index.js",
        "page.jsx",
        "style.css",
        "layout.html",
        "readme.md",
        "config.toml",
        "data.yaml",
        "schema.yml",
        "data.json",
        "query.sql",
        "Makefile",
        "Dockerfile",
        ".gitignore",
    ])
    def test_safe_extensions_pass(self, tmp_path: Path, name: str) -> None:
        _touch(tmp_path, f"src/{name}", "content")
        sf = _sf(tmp_path)
        result = sf.is_excluded(f"src/{name}")
        assert not result.excluded, f"{name} should not be excluded but got: {result.reason}"

    def test_json_file_not_excluded(self, tmp_path: Path) -> None:
        """A plain JSON file (not named credentials/secrets) should pass."""
        _touch(tmp_path, "package.json", '{"name": "app"}')
        sf = _sf(tmp_path)
        result = sf.is_excluded("package.json")
        assert not result.excluded

    def test_yaml_config_safe(self, tmp_path: Path) -> None:
        _touch(tmp_path, "docker-compose.yml", "version: '3'")
        sf = _sf(tmp_path)
        result = sf.is_excluded("docker-compose.yml")
        assert not result.excluded

    def test_toml_config_safe(self, tmp_path: Path) -> None:
        _touch(tmp_path, "pyproject.toml", "[tool]\nname = 'app'")
        sf = _sf(tmp_path)
        result = sf.is_excluded("pyproject.toml")
        assert not result.excluded


# ===================================================================
# 6. Content Heuristic (Layer 4)
# ===================================================================

class TestContentHeuristic:
    """Content heuristic catches secrets not caught by path/name/type."""

    def test_fake_api_key(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/config.py", 'API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"')
        sf = _sf(tmp_path)
        result = sf.is_excluded("src/config.py")
        assert result.excluded
        assert "sensitive content" in result.reason.lower()

    def test_fake_secret_key(self, tmp_path: Path) -> None:
        _touch(tmp_path, "settings.py", 'SECRET_KEY = "mysecretkeyvalue1234567890ab"')
        sf = _sf(tmp_path)
        result = sf.is_excluded("settings.py")
        assert result.excluded
        assert "sensitive content" in result.reason.lower()

    def test_fake_access_token(self, tmp_path: Path) -> None:
        _touch(tmp_path, "auth.py", 'ACCESS_TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXyz1234"')
        sf = _sf(tmp_path)
        result = sf.is_excluded("auth.py")
        assert result.excluded
        assert "sensitive content" in result.reason.lower()

    def test_fake_password(self, tmp_path: Path) -> None:
        _touch(tmp_path, "db.py", 'password = "SuperSecret12345678"')
        sf = _sf(tmp_path)
        result = sf.is_excluded("db.py")
        assert result.excluded

    def test_fake_client_secret(self, tmp_path: Path) -> None:
        _touch(tmp_path, "oauth.py", 'CLIENT_SECRET = "abcdef1234567890abcdef12"')
        sf = _sf(tmp_path)
        result = sf.is_excluded("oauth.py")
        assert result.excluded

    def test_fake_auth_token(self, tmp_path: Path) -> None:
        _touch(tmp_path, "session.py", 'AUTH_TOKEN = "eyJhbGciOiJIUzI1NiJ9.payload.signature"')
        sf = _sf(tmp_path)
        result = sf.is_excluded("session.py")
        assert result.excluded

    def test_private_key_marker(self, tmp_path: Path) -> None:
        content = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQ..."
        _touch(tmp_path, "key.py", content)
        sf = _sf(tmp_path)
        result = sf.is_excluded("key.py")
        assert result.excluded
        assert "sensitive content" in result.reason.lower()

    def test_private_key_header_ec(self, tmp_path: Path) -> None:
        content = "-----BEGIN EC PRIVATE KEY-----\nMIIB..."
        _touch(tmp_path, "ec.py", content)
        sf = _sf(tmp_path)
        result = sf.is_excluded("ec.py")
        assert result.excluded

    def test_private_key_assignment(self, tmp_path: Path) -> None:
        _touch(tmp_path, "k.py", 'private_key = "somevalue"')
        sf = _sf(tmp_path)
        result = sf.is_excluded("k.py")
        assert result.excluded

    def test_clean_file_no_heuristic_trigger(self, tmp_path: Path) -> None:
        content = (
            "def hello():\n"
            "    return 'world'\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    print(hello())\n"
        )
        _touch(tmp_path, "src/app.py", content)
        sf = _sf(tmp_path)
        result = sf.is_excluded("src/app.py")
        assert not result.excluded

    def test_json_with_api_key(self, tmp_path: Path) -> None:
        content = '{"api_key": "abcdefghijklmnopqrstuvwxyz12345678"}'
        _touch(tmp_path, "config/app.json", content)
        sf = _sf(tmp_path)
        result = sf.is_excluded("config/app.json")
        assert result.excluded
        assert "sensitive content" in result.reason.lower()

    def test_yaml_with_secret(self, tmp_path: Path) -> None:
        content = "database:\n  password: SuperSecret12345678\n"
        _touch(tmp_path, "config/db.yml", content)
        sf = _sf(tmp_path)
        result = sf.is_excluded("config/db.yml")
        assert result.excluded

    def test_toml_with_token(self, tmp_path: Path) -> None:
        content = "[auth]\ntoken = \"ghp_ABCDEFGHIJKLMNOPQRSTUVWXyz1234\"\n"
        _touch(tmp_path, "config/auth.toml", content)
        sf = _sf(tmp_path)
        result = sf.is_excluded("config/auth.toml")
        assert result.excluded

    def test_binary_content_auto_excluded(self, tmp_path: Path) -> None:
        """A .py file with null bytes should be caught as binary."""
        _touch(tmp_path, "weird.py", b"\x00\x00\x00\x00\x00binary")
        sf = _sf(tmp_path)
        result = sf.is_excluded("weird.py")
        assert result.excluded
        assert "binary" in result.reason.lower()

    def test_empty_file_not_excluded(self, tmp_path: Path) -> None:
        _touch(tmp_path, "empty.py", b"")
        sf = _sf(tmp_path)
        result = sf.is_excluded("empty.py")
        assert not result.excluded


# ===================================================================
# 7. Binary / Large File Handling
# ===================================================================

class TestBinaryAndLargeFiles:
    """Binary and oversized files must be excluded."""

    def test_png_magic_bytes(self, tmp_path: Path) -> None:
        _touch(tmp_path, "image.py", b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        sf = _sf(tmp_path)
        result = sf.is_excluded("image.py")
        assert result.excluded

    def test_pdf_magic_bytes(self, tmp_path: Path) -> None:
        _touch(tmp_path, "data.py", b"%PDF-1.4" + b"\x00" * 100)
        sf = _sf(tmp_path)
        result = sf.is_excluded("data.py")
        assert result.excluded

    def test_zip_magic_bytes(self, tmp_path: Path) -> None:
        _touch(tmp_path, "archive.py", b"PK\x03\x04" + b"\x00" * 100)
        sf = _sf(tmp_path)
        result = sf.is_excluded("archive.py")
        assert result.excluded

    def test_elf_magic_bytes(self, tmp_path: Path) -> None:
        _touch(tmp_path, "binary.py", b"\x7fELF" + b"\x00" * 100)
        sf = _sf(tmp_path)
        result = sf.is_excluded("binary.py")
        assert result.excluded

    def test_oversized_file(self, tmp_path: Path) -> None:
        sf = _sf(tmp_path, max_file_size=1024)
        big = tmp_path / "large.py"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_bytes(b"x" * 2048)
        result = sf.is_excluded("large.py")
        assert result.excluded
        assert "too large" in result.reason.lower()

    def test_file_at_exact_limit(self, tmp_path: Path) -> None:
        sf = _sf(tmp_path, max_file_size=100)
        f = tmp_path / "exact.py"
        f.write_bytes(b"a" * 100)
        result = sf.is_excluded("exact.py")
        assert not result.excluded

    def test_file_one_byte_over_limit(self, tmp_path: Path) -> None:
        sf = _sf(tmp_path, max_file_size=100)
        f = tmp_path / "over.py"
        f.write_bytes(b"a" * 101)
        result = sf.is_excluded("over.py")
        assert result.excluded

    def test_oversized_checked_after_name(self, tmp_path: Path) -> None:
        """If name is already sensitive, size doesn't matter — excluded by name."""
        sf = _sf(tmp_path, max_file_size=10)
        f = tmp_path / ".env"
        f.write_bytes(b"SECRET=x" * 2)
        result = sf.is_excluded(".env")
        assert result.excluded
        assert "sensitive" in result.reason.lower()


# ===================================================================
# 8. Secret Leakage Protection
# ===================================================================

class TestNoSecretLeakage:
    """Secret-like values must never appear in results/reasons."""

    FAKE_SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"

    def test_secret_not_in_reason(self, tmp_path: Path) -> None:
        _touch(tmp_path, "config.py", f'API_KEY = "{self.FAKE_SECRET}"')
        sf = _sf(tmp_path)
        result = sf.is_excluded("config.py")
        assert result.excluded
        assert self.FAKE_SECRET not in result.reason
        assert self.FAKE_SECRET not in str(result)

    def test_secret_not_in_str_representation(self, tmp_path: Path) -> None:
        _touch(tmp_path, "auth.py", f'password = "{self.FAKE_SECRET}"')
        sf = _sf(tmp_path)
        result = sf.is_excluded("auth.py")
        result_str = str(result)
        assert self.FAKE_SECRET not in result_str
        assert "sensitive content" in result_str.lower() or "password" in result_str.lower()

    def test_env_value_not_leaked(self, tmp_path: Path) -> None:
        env_content = "DATABASE_URL=postgres://admin:SuperSecret123@host/db"
        _touch(tmp_path, ".env", env_content)
        sf = _sf(tmp_path)
        result = sf.is_excluded(".env")
        assert "SuperSecret123" not in result.reason
        assert "SuperSecret123" not in str(result)

    def test_private_key_not_in_reason(self, tmp_path: Path) -> None:
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA..."
        _touch(tmp_path, "key.py", pem)
        sf = _sf(tmp_path)
        result = sf.is_excluded("key.py")
        assert "MIIEpAIBAAKCAQEA" not in result.reason
        assert "BEGIN RSA PRIVATE" not in result.reason

    def test_filter_results_batch_no_leakage(self, tmp_path: Path) -> None:
        _touch(tmp_path, ".env", "SECRET=abcdef1234567890")
        sf = _sf(tmp_path)
        results = sf.filter_paths([".env", "src/app.py", "id_rsa"])
        for r in results:
            assert "abcdef1234567890" not in str(r)
            assert "abcdef1234567890" not in r.reason


# ===================================================================
# 9. API Design
# ===================================================================

class TestAPIDesign:
    """Verify the public API surface."""

    def test_is_excluded_returns_filter_result(self, tmp_path: Path) -> None:
        sf = _sf(tmp_path)
        result = sf.is_excluded(".env")
        assert isinstance(result, FilterResult)
        assert result.excluded is True
        assert isinstance(result.reason, str)

    def test_should_include_inverse(self, tmp_path: Path) -> None:
        sf = _sf(tmp_path)
        assert sf.should_include("src/app.py") is True
        assert sf.should_include(".env") is False

    def test_filter_paths_returns_list(self, tmp_path: Path) -> None:
        sf = _sf(tmp_path)
        results = sf.filter_paths([".env", "src/app.py", "id_rsa"])
        assert isinstance(results, list)
        assert len(results) == 3
        assert all(isinstance(r, FilterResult) for r in results)

    def test_filter_paths_preserves_order(self, tmp_path: Path) -> None:
        sf = _sf(tmp_path)
        paths = [".env", "src/app.py", "id_rsa", "README.md"]
        results = sf.filter_paths(paths)
        assert [r.path for r in results] == paths

    def test_filter_result_str_representation(self, tmp_path: Path) -> None:
        r = FilterResult(path="test.py", excluded=False)
        assert "INCLUDE" in str(r)
        r2 = FilterResult(path="secret.py", excluded=True, reason="test reason")
        s = str(r2)
        assert "SKIP" in s
        assert "secret.py" in s
        assert "test reason" in s

    def test_root_not_existing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            SecurityFilter(project_root=tmp_path / "nonexistent")

    def test_extra_excluded_dirs(self, tmp_path: Path) -> None:
        sf = _sf(tmp_path, extra_excluded_dirs=frozenset({"custom_cache"}))
        _touch(tmp_path, "custom_cache/file.py", "x")
        result = sf.is_excluded("custom_cache/file.py")
        assert result.excluded

    def test_extra_excluded_extensions(self, tmp_path: Path) -> None:
        sf = _sf(tmp_path, extra_excluded_extensions=frozenset({".xyz"}))
        _touch(tmp_path, "data.xyz", "x")
        result = sf.is_excluded("data.xyz")
        assert result.excluded


# ===================================================================
# 10. Case Sensitivity Edge Cases
# ===================================================================

class TestCaseSensitivity:
    """Sensitive filenames should be caught regardless of case."""

    def test_uppercase_env(self, tmp_path: Path) -> None:
        _touch(tmp_path, ".ENV", "SECRET=x")
        sf = _sf(tmp_path)
        result = sf.is_excluded(".ENV")
        assert result.excluded

    def test_mixed_case_env(self, tmp_path: Path) -> None:
        _touch(tmp_path, ".Env", "SECRET=x")
        sf = _sf(tmp_path)
        result = sf.is_excluded(".Env")
        assert result.excluded

    def test_uppercase_pem(self, tmp_path: Path) -> None:
        _touch(tmp_path, "cert.PEM", "data")
        sf = _sf(tmp_path)
        result = sf.is_excluded("cert.PEM")
        assert result.excluded

    def test_uppercase_node_modules(self, tmp_path: Path) -> None:
        _touch(tmp_path, "NODE_MODULES/pkg/index.js", "x")
        sf = _sf(tmp_path)
        result = sf.is_excluded("NODE_MODULES/pkg/index.js")
        assert result.excluded

    def test_mixed_case_git(self, tmp_path: Path) -> None:
        _touch(tmp_path, ".Git/config", "x")
        sf = _sf(tmp_path)
        result = sf.is_excluded(".Git/config")
        assert result.excluded


# ===================================================================
# 11. Nested Sensitive Files
# ===================================================================

class TestNestedSensitiveFiles:
    """Sensitive files in nested directories must be caught."""

    def test_env_in_deeply_nested_path(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/config/prod/.env", "SECRET=x")
        sf = _sf(tmp_path)
        result = sf.is_excluded("src/config/prod/.env")
        assert result.excluded

    def test_pem_in_certs_deep(self, tmp_path: Path) -> None:
        _touch(tmp_path, "certs/ca/intermediate/server.pem", "data")
        sf = _sf(tmp_path)
        result = sf.is_excluded("certs/ca/intermediate/server.pem")
        assert result.excluded

    def test_id_rsa_in_ssh_dir(self, tmp_path: Path) -> None:
        _touch(tmp_path, "ssh/id_rsa", "keydata")
        sf = _sf(tmp_path)
        result = sf.is_excluded("ssh/id_rsa")
        assert result.excluded

    def test_credentials_in_dot_dir(self, tmp_path: Path) -> None:
        _touch(tmp_path, ".aws/credentials", "[default]\naws_access_key_id = FAKE")
        sf = _sf(tmp_path)
        result = sf.is_excluded(".aws/credentials")
        assert result.excluded


# ===================================================================
# 12. Edge Cases
# ===================================================================

class TestEdgeCases:
    """Various edge cases and boundary conditions."""

    def test_root_path_itself(self, tmp_path: Path) -> None:
        sf = _sf(tmp_path)
        result = sf.is_excluded(".")
        assert not result.excluded

    def test_empty_string_path(self, tmp_path: Path) -> None:
        sf = _sf(tmp_path)
        result = sf.is_excluded("")
        # Empty string resolves to root — should not be excluded
        assert not result.excluded

    def test_path_with_spaces(self, tmp_path: Path) -> None:
        _touch(tmp_path, "my folder/file.py", "code")
        sf = _sf(tmp_path)
        result = sf.is_excluded("my folder/file.py")
        assert not result.excluded

    def test_path_with_special_chars(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/@scope/package/index.ts", "export {}")
        sf = _sf(tmp_path)
        result = sf.is_excluded("src/@scope/package/index.ts")
        assert not result.excluded

    def test_file_not_existing(self, tmp_path: Path) -> None:
        """Non-existent file should still be filterable by name/path."""
        sf = _sf(tmp_path)
        result = sf.is_excluded(".env")
        assert result.excluded
        assert "sensitive" in result.reason.lower()

    def test_gitignore_excluded(self, tmp_path: Path) -> None:
        """A non-existent .env should still be caught by name."""
        sf = _sf(tmp_path)
        result = sf.is_excluded("subdir/.env.production")
        assert result.excluded

    def test_multiple_filter_calls_consistent(self, tmp_path: Path) -> None:
        sf = _sf(tmp_path)
        r1 = sf.is_excluded(".env")
        r2 = sf.is_excluded(".env")
        assert r1.excluded == r2.excluded
        assert r1.reason == r2.reason


# ===================================================================
# 13. Defense in Depth Ordering
# ===================================================================

class TestDefenseInDepthOrder:
    """Verify that exclusion reasons are correct even when multiple layers match."""

    def test_name_beats_content(self, tmp_path: Path) -> None:
        """A .env file should be excluded by name, not by content."""
        _touch(tmp_path, ".env", "harmless content here")
        sf = _sf(tmp_path)
        result = sf.is_excluded(".env")
        assert result.excluded
        assert "sensitive" in result.reason.lower()
        assert "content" not in result.reason.lower()

    def test_extension_beats_content(self, tmp_path: Path) -> None:
        """A .png file should be excluded by extension, not content."""
        _touch(tmp_path, "image.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
        sf = _sf(tmp_path)
        result = sf.is_excluded("image.png")
        assert result.excluded
        assert "extension" in result.reason.lower()

    def test_dir_beats_name(self, tmp_path: Path) -> None:
        """A .env inside node_modules should be excluded by directory."""
        _touch(tmp_path, "node_modules/.env", "SECRET=x")
        sf = _sf(tmp_path)
        result = sf.is_excluded("node_modules/.env")
        assert result.excluded
        assert "directory" in result.reason.lower()

    def test_name_beats_size(self, tmp_path: Path) -> None:
        """A .env file should be excluded by name even if oversized."""
        sf = _sf(tmp_path, max_file_size=10)
        f = tmp_path / ".env"
        f.write_bytes(b"SECRET=x" * 100)
        result = sf.is_excluded(".env")
        assert result.excluded
        assert "sensitive" in result.reason.lower()


# ===================================================================
# 14. FilterResult string safety
# ===================================================================

class TestFilterResultStringSafety:
    """FilterResult.__str__ must never leak sensitive content."""

    def test_str_no_secret_in_output(self, tmp_path: Path) -> None:
        secret = "sk-test-abcdefghijklmnopqrstuvwxyz123456"
        _touch(tmp_path, "config.py", f'API_KEY = "{secret}"')
        sf = _sf(tmp_path)
        result = sf.is_excluded("config.py")
        output = str(result)
        assert secret not in output

    def test_batch_output_safe(self, tmp_path: Path) -> None:
        _touch(tmp_path, ".env", "PASSWORD=hunter2SuperSecret")
        _touch(tmp_path, "id_rsa", "-----BEGIN RSA PRIVATE KEY-----")
        sf = _sf(tmp_path)
        results = sf.filter_paths([".env", "id_rsa", "src/app.py"])
        for r in results:
            assert "hunter2SuperSecret" not in str(r)
            assert "BEGIN RSA PRIVATE" not in str(r)


# ===================================================================
# 15. Integration readiness (API contract for Phase 4)
# ===================================================================

class TestIntegrationReadiness:
    """Verify the API is ready for Context Builder (Phase 4)."""

    def test_filter_paths_with_mixed_types(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/main.py", "print('hello')")
        _touch(tmp_path, ".env", "SECRET=x")
        _touch(tmp_path, "id_rsa", "key")
        _touch(tmp_path, "image.png", b"\x89PNG")

        sf = _sf(tmp_path)
        results = sf.filter_paths([
            "src/main.py",
            ".env",
            "id_rsa",
            "image.png",
        ])

        included = [r for r in results if not r.excluded]
        excluded = [r for r in results if r.excluded]

        assert len(included) == 1
        assert included[0].path == "src/main.py"
        assert len(excluded) == 3

    def test_results_are_deterministic(self, tmp_path: Path) -> None:
        sf = _sf(tmp_path)
        paths = [".env", "src/app.py", "config.json", "id_rsa", "README.md"]
        r1 = sf.filter_paths(paths)
        r2 = sf.filter_paths(paths)
        assert [(r.path, r.excluded) for r in r1] == [(r.path, r.excluded) for r in r2]

    def test_project_root_immutable(self, tmp_path: Path) -> None:
        sf = _sf(tmp_path)
        original_root = sf._root
        _touch(tmp_path, ".env", "x")
        sf.is_excluded(".env")
        assert sf._root == original_root
