"""Tests for hermes_cli.profile_distribution — packaged profile installs.

Covers manifest parsing, version requirement checks, pack → install round
trip, update semantics (config preserved by default, user data never touched),
and security (credentials excluded from packed archives, path-traversal
rejection in archives).
"""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

import pytest

from hermes_cli.profile_distribution import (
    DEFAULT_DIST_OWNED,
    DistributionError,
    DistributionManifest,
    EnvRequirement,
    MANIFEST_FILENAME,
    USER_OWNED_EXCLUDE,
    _env_template_from_manifest,
    _find_dist_root,
    _parse_semver,
    check_hermes_requires,
    describe_distribution,
    install_distribution,
    pack_profile,
    plan_install,
    read_manifest,
    update_distribution,
    write_manifest,
)


# ---------------------------------------------------------------------------
# Isolated profile env (matches tests/hermes_cli/test_profiles.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


def _make_profile(profile_env, name: str = "source_profile") -> Path:
    """Create a minimal profile under the isolated HERMES_HOME."""
    from hermes_cli.profiles import create_profile

    profile_dir = create_profile(name=name, no_alias=True)
    # Lay down representative content
    (profile_dir / "SOUL.md").write_text("I am Source.\n")
    (profile_dir / "config.yaml").write_text("model:\n  model: gpt-4\n")
    (profile_dir / "mcp.json").write_text('{"servers": {}}\n')
    (profile_dir / "skills").mkdir(exist_ok=True)
    (profile_dir / "skills" / "demo").mkdir(exist_ok=True)
    (profile_dir / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: test\n---\n# Demo skill\n"
    )
    (profile_dir / "cron").mkdir(exist_ok=True)
    (profile_dir / "cron" / "daily.json").write_text('{"schedule": "0 9 * * *"}')
    # User-owned data that MUST NOT ship in the archive
    (profile_dir / "memories").mkdir(exist_ok=True)
    (profile_dir / "memories" / "MEMORY.md").write_text("# secret memory\n")
    (profile_dir / "auth.json").write_text('{"tokens": "sk-secret"}')
    (profile_dir / ".env").write_text("OPENAI_API_KEY=sk-real\n")
    return profile_dir


# ===========================================================================
# Manifest parsing
# ===========================================================================


class TestManifestParsing:

    def test_env_requirement_from_dict_minimal(self):
        er = EnvRequirement.from_dict({"name": "FOO"})
        assert er.name == "FOO"
        assert er.required is True
        assert er.default is None

    def test_env_requirement_missing_name_raises(self):
        with pytest.raises(DistributionError, match="missing 'name'"):
            EnvRequirement.from_dict({"description": "no name"})

    def test_env_requirement_optional_with_default(self):
        er = EnvRequirement.from_dict(
            {"name": "X", "required": False, "default": "http://localhost"}
        )
        assert er.required is False
        assert er.default == "http://localhost"

    def test_manifest_from_dict_full(self):
        m = DistributionManifest.from_dict({
            "name": "telemetry",
            "version": "0.2.0",
            "hermes_requires": ">=0.12.0",
            "env_requires": [
                {"name": "A", "description": "a key"},
                {"name": "B", "required": False, "default": "x"},
            ],
        })
        assert m.name == "telemetry"
        assert m.version == "0.2.0"
        assert len(m.env_requires) == 2
        assert m.env_requires[1].required is False

    def test_manifest_missing_name_raises(self):
        with pytest.raises(DistributionError, match="missing 'name'"):
            DistributionManifest.from_dict({"version": "1.0"})

    def test_manifest_owned_paths_default(self):
        m = DistributionManifest(name="x")
        assert tuple(m.owned_paths()) == DEFAULT_DIST_OWNED

    def test_manifest_owned_paths_override(self):
        m = DistributionManifest(name="x", distribution_owned=["SOUL.md", "mcp.json"])
        assert m.owned_paths() == ["SOUL.md", "mcp.json"]

    def test_read_manifest_missing_returns_none(self, tmp_path):
        assert read_manifest(tmp_path) is None

    def test_write_then_read_roundtrip(self, tmp_path):
        m = DistributionManifest(
            name="demo",
            version="1.2.3",
            description="hi",
            hermes_requires=">=0.12.0",
            env_requires=[EnvRequirement(name="KEY", description="d")],
        )
        write_manifest(tmp_path, m)
        loaded = read_manifest(tmp_path)
        assert loaded is not None
        assert loaded.name == "demo"
        assert loaded.version == "1.2.3"
        assert loaded.env_requires[0].name == "KEY"


# ===========================================================================
# Version requirement checks
# ===========================================================================


class TestVersionRequires:

    def test_parse_semver_simple(self):
        assert _parse_semver("0.12.0") == (0, 12, 0)
        assert _parse_semver("v1.2.3") == (1, 2, 3)
        assert _parse_semver("1.2.3-rc1") == (1, 2, 3)
        assert _parse_semver("0.12") == (0, 12, 0)

    def test_parse_semver_bad_raises(self):
        with pytest.raises(DistributionError):
            _parse_semver("not-a-version")

    def test_gte_satisfied(self):
        check_hermes_requires(">=0.12.0", "0.12.0")
        check_hermes_requires(">=0.12.0", "0.13.0")
        check_hermes_requires(">=0.12.0", "1.0.0")

    def test_gte_not_satisfied(self):
        with pytest.raises(DistributionError, match=r"requires Hermes >=0\.13\.0"):
            check_hermes_requires(">=0.13.0", "0.12.0")

    def test_eq_exact(self):
        check_hermes_requires("==0.12.0", "0.12.0")
        with pytest.raises(DistributionError):
            check_hermes_requires("==0.12.0", "0.12.1")

    def test_lt_op(self):
        check_hermes_requires("<1.0.0", "0.12.0")
        with pytest.raises(DistributionError):
            check_hermes_requires("<1.0.0", "1.0.0")

    def test_bare_version_treated_as_gte(self):
        check_hermes_requires("0.12.0", "0.13.0")
        with pytest.raises(DistributionError):
            check_hermes_requires("0.13.0", "0.12.0")

    def test_empty_spec_is_noop(self):
        check_hermes_requires("", "0.0.1")
        check_hermes_requires(None, "0.0.1")  # type: ignore[arg-type]


# ===========================================================================
# Env template rendering
# ===========================================================================


class TestEnvTemplate:

    def test_required_key_uncommented(self):
        m = DistributionManifest(
            name="x",
            env_requires=[EnvRequirement(name="API", description="api key")],
        )
        body = _env_template_from_manifest(m)
        assert "API=" in body
        # Required vars must NOT be commented out
        assert "\nAPI=" in body or body.startswith("API=")

    def test_optional_key_commented(self):
        m = DistributionManifest(
            name="x",
            env_requires=[
                EnvRequirement(name="OPT", required=False, default="http://x"),
            ],
        )
        body = _env_template_from_manifest(m)
        # Optional keys are commented out so they don't override env by default
        assert "# OPT=http://x" in body


# ===========================================================================
# Pack — archive creation
# ===========================================================================


class TestPack:

    def test_pack_emits_archive_with_manifest(self, profile_env):
        _make_profile(profile_env, "src")
        output = profile_env / "src.tar.gz"
        result = pack_profile("src", str(output))
        assert result.exists()
        with tarfile.open(result, "r:gz") as tf:
            names = tf.getnames()
        assert any(n.endswith(f"src/{MANIFEST_FILENAME}") for n in names)
        assert any(n.endswith("src/SOUL.md") for n in names)
        assert any(n.endswith("src/skills/demo/SKILL.md") for n in names)

    def test_pack_excludes_credentials(self, profile_env):
        _make_profile(profile_env, "src")
        output = profile_env / "src.tar.gz"
        result = pack_profile("src", str(output))
        with tarfile.open(result, "r:gz") as tf:
            names = tf.getnames()
        assert not any(n.endswith("auth.json") for n in names), "auth.json leaked"
        assert not any(n.endswith(".env") for n in names), ".env leaked"

    def test_pack_excludes_user_data(self, profile_env):
        _make_profile(profile_env, "src")
        result = pack_profile("src", str(profile_env / "src.tar.gz"))
        with tarfile.open(result, "r:gz") as tf:
            names = tf.getnames()
        # memories/ is user-owned and must not be shipped
        assert not any("memories/" in n for n in names), \
            "memories/ leaked into distribution"

    def test_pack_uses_explicit_manifest(self, profile_env):
        _make_profile(profile_env, "src")
        m = DistributionManifest(
            name="renamed",
            version="2.0.0",
            description="hello",
            env_requires=[EnvRequirement(name="FOO")],
        )
        result = pack_profile("src", str(profile_env / "out.tar.gz"), manifest=m)
        # Archive root is manifest.name, not profile name
        with tarfile.open(result, "r:gz") as tf:
            names = tf.getnames()
        assert any(n.startswith("renamed/") for n in names)
        # .env.template should be emitted because env_requires is non-empty
        assert any(n.endswith("renamed/.env.template") for n in names)

    def test_pack_strips_source_field(self, profile_env):
        _make_profile(profile_env, "src")
        # Pre-seed a manifest with a source field; pack should wipe it before
        # writing to the archive (source is user-local provenance).
        profile_dir = Path(os.environ["HERMES_HOME"]).parent / ".hermes" / "profiles" / "src"
        write_manifest(
            profile_dir,
            DistributionManifest(name="src", version="0.1.0", source="/home/me/src"),
        )
        result = pack_profile("src", str(profile_env / "src.tar.gz"))
        with tarfile.open(result, "r:gz") as tf:
            member = tf.getmember(f"src/{MANIFEST_FILENAME}")
            data = tf.extractfile(member).read().decode()
        assert "/home/me/src" not in data


# ===========================================================================
# Find dist root — archive layout resolution
# ===========================================================================


class TestFindDistRoot:

    def test_manifest_at_root(self, tmp_path):
        (tmp_path / MANIFEST_FILENAME).write_text("name: x\nversion: 1\n")
        assert _find_dist_root(tmp_path) == tmp_path

    def test_manifest_inside_single_subdir(self, tmp_path):
        sub = tmp_path / "distro"
        sub.mkdir()
        (sub / MANIFEST_FILENAME).write_text("name: x\nversion: 1\n")
        assert _find_dist_root(tmp_path) == sub

    def test_no_manifest_raises(self, tmp_path):
        (tmp_path / "only_a_readme.md").write_text("hello")
        with pytest.raises(DistributionError, match="No distribution.yaml"):
            _find_dist_root(tmp_path)


# ===========================================================================
# Install — fresh and force
# ===========================================================================


class TestInstall:

    def test_install_from_local_tarball(self, profile_env):
        _make_profile(profile_env, "src")
        archive = pack_profile("src", str(profile_env / "src.tar.gz"))

        plan = install_distribution(str(archive), name="installed")
        assert plan.target_dir.is_dir()
        assert (plan.target_dir / "SOUL.md").read_text() == "I am Source.\n"
        assert (plan.target_dir / "skills" / "demo" / "SKILL.md").exists()
        # Manifest on disk records canonical name + provenance
        m = read_manifest(plan.target_dir)
        assert m.name == "installed"
        assert m.source.endswith("src.tar.gz")

    def test_install_from_directory(self, profile_env):
        _make_profile(profile_env, "src")
        archive = pack_profile("src", str(profile_env / "src.tar.gz"))
        # Extract to a dir and install from there
        staging = profile_env / "staged"
        with tarfile.open(archive, "r:gz") as tf:
            # Manual safe extract — only for tests
            for m in tf.getmembers():
                if m.isfile():
                    target = staging / m.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    extracted = tf.extractfile(m)
                    target.write_bytes(extracted.read())
                elif m.isdir():
                    (staging / m.name).mkdir(parents=True, exist_ok=True)

        plan = install_distribution(str(staging / "src"), name="fromdir")
        assert plan.target_dir.is_dir()
        assert (plan.target_dir / "SOUL.md").exists()

    def test_install_does_not_include_credentials(self, profile_env):
        _make_profile(profile_env, "src")
        archive = pack_profile("src", str(profile_env / "src.tar.gz"))
        plan = install_distribution(str(archive), name="clean")
        # The archive never had auth.json or .env so they shouldn't appear
        assert not (plan.target_dir / "auth.json").exists()
        assert not (plan.target_dir / ".env").exists()

    def test_install_rejects_existing_without_force(self, profile_env):
        _make_profile(profile_env, "src")
        archive = pack_profile("src", str(profile_env / "src.tar.gz"))
        install_distribution(str(archive), name="existing")
        with pytest.raises(DistributionError, match="already exists"):
            install_distribution(str(archive), name="existing")

    def test_install_with_force_overwrites(self, profile_env):
        _make_profile(profile_env, "src")
        archive = pack_profile("src", str(profile_env / "src.tar.gz"))
        install_distribution(str(archive), name="target")
        # Install again with --force succeeds
        plan = install_distribution(str(archive), name="target", force=True)
        assert plan.target_dir.is_dir()

    def test_install_rejects_default_name(self, profile_env):
        _make_profile(profile_env, "src")
        archive = pack_profile("src", str(profile_env / "src.tar.gz"))
        with pytest.raises(DistributionError, match="Cannot install"):
            install_distribution(str(archive), name="default")

    def test_install_rejects_non_distribution(self, profile_env, tmp_path):
        # Make a tar.gz with no manifest
        bogus = tmp_path / "bogus_dir"
        bogus.mkdir()
        (bogus / "some_file").write_text("hi")
        archive = tmp_path / "bogus.tar.gz"
        import shutil
        shutil.make_archive(str(archive).removesuffix(".tar.gz"), "gztar", str(tmp_path), "bogus_dir")

        with pytest.raises(DistributionError, match="No distribution.yaml"):
            plan_install(str(archive), tmp_path / "work", override_name="x")


# ===========================================================================
# Update — preserves user data, preserves config by default
# ===========================================================================


class TestUpdate:

    def test_update_preserves_user_data(self, profile_env):
        # 1. Make source, pack, install
        _make_profile(profile_env, "src")
        archive = pack_profile("src", str(profile_env / "src.tar.gz"))
        plan = install_distribution(str(archive), name="telem")

        # 2. Add user-owned data to the installed profile
        (plan.target_dir / "memories" / "MEMORY.md").write_text("# USER MEMORY\n")
        (plan.target_dir / ".env").write_text("OPENAI_API_KEY=sk-user\n")
        (plan.target_dir / "auth.json").write_text('{"user": "auth"}')
        (plan.target_dir / "sessions").mkdir(exist_ok=True)
        (plan.target_dir / "sessions" / "chat.json").write_text('{"s": 1}')

        # 3. Bump source — change SOUL, leave source tar.gz at same path
        src_profile = Path(os.environ["HERMES_HOME"]).parent / ".hermes" / "profiles" / "src"
        (src_profile / "SOUL.md").write_text("I am Source v2.\n")
        pack_profile("src", str(archive))

        # 4. Update
        update_distribution("telem", force_config=False)

        # 5. Dist-owned changed
        assert (plan.target_dir / "SOUL.md").read_text() == "I am Source v2.\n"
        # 6. User-owned preserved
        assert (plan.target_dir / "memories" / "MEMORY.md").read_text() == "# USER MEMORY\n"
        assert (plan.target_dir / ".env").read_text() == "OPENAI_API_KEY=sk-user\n"
        assert (plan.target_dir / "auth.json").read_text() == '{"user": "auth"}'
        assert (plan.target_dir / "sessions" / "chat.json").read_text() == '{"s": 1}'

    def test_update_preserves_config_by_default(self, profile_env):
        _make_profile(profile_env, "src")
        archive = pack_profile("src", str(profile_env / "src.tar.gz"))
        plan = install_distribution(str(archive), name="t2")

        # User edits config
        (plan.target_dir / "config.yaml").write_text("model:\n  model: gpt-5\n# user override\n")

        # Bump source config
        src_profile = Path(os.environ["HERMES_HOME"]).parent / ".hermes" / "profiles" / "src"
        (src_profile / "config.yaml").write_text("model:\n  model: claude\n")
        pack_profile("src", str(archive))

        update_distribution("t2", force_config=False)
        assert "gpt-5" in (plan.target_dir / "config.yaml").read_text()
        assert "user override" in (plan.target_dir / "config.yaml").read_text()

    def test_update_force_config_overwrites(self, profile_env):
        _make_profile(profile_env, "src")
        archive = pack_profile("src", str(profile_env / "src.tar.gz"))
        plan = install_distribution(str(archive), name="t3")

        (plan.target_dir / "config.yaml").write_text("model:\n  model: gpt-5\n")

        src_profile = Path(os.environ["HERMES_HOME"]).parent / ".hermes" / "profiles" / "src"
        (src_profile / "config.yaml").write_text("model:\n  model: claude\n")
        pack_profile("src", str(archive))

        update_distribution("t3", force_config=True)
        assert "claude" in (plan.target_dir / "config.yaml").read_text()
        assert "gpt-5" not in (plan.target_dir / "config.yaml").read_text()

    def test_update_missing_manifest_errors(self, profile_env):
        # Make a profile without a manifest; update must refuse
        from hermes_cli.profiles import create_profile
        profile_dir = create_profile(name="plain", no_alias=True)
        with pytest.raises(DistributionError, match="not a distribution"):
            update_distribution("plain")


# ===========================================================================
# describe_distribution — info subcommand
# ===========================================================================


class TestDescribe:

    def test_describe_existing_distribution(self, profile_env):
        _make_profile(profile_env, "src")
        archive = pack_profile(
            "src",
            str(profile_env / "src.tar.gz"),
            manifest=DistributionManifest(
                name="telem",
                version="1.0.0",
                description="compliance monitor",
                env_requires=[EnvRequirement(name="API", description="api key")],
            ),
        )
        install_distribution(str(archive), name="telem")
        data = describe_distribution("telem")
        assert data["name"] == "telem"
        assert data["version"] == "1.0.0"
        assert data["env_requires"][0]["name"] == "API"

    def test_describe_non_distribution_returns_empty(self, profile_env):
        from hermes_cli.profiles import create_profile
        create_profile(name="plain", no_alias=True)
        assert describe_distribution("plain") == {}

    def test_describe_missing_profile_raises(self, profile_env):
        with pytest.raises(DistributionError, match="does not exist"):
            describe_distribution("nonexistent")


# ===========================================================================
# Security — archive traversal, source strip
# ===========================================================================


class TestSecurity:

    def test_path_traversal_rejected(self, profile_env, tmp_path):
        # Craft a malicious tar.gz with ``../escape`` members
        malicious = tmp_path / "evil.tar.gz"
        with tarfile.open(malicious, "w:gz") as tf:
            info = tarfile.TarInfo(name="evil/../../../escape.txt")
            data = b"pwn"
            info.size = len(data)
            import io
            tf.addfile(info, io.BytesIO(data))
            mf = tarfile.TarInfo(name="evil/distribution.yaml")
            mdata = b"name: evil\nversion: 1\n"
            mf.size = len(mdata)
            tf.addfile(mf, io.BytesIO(mdata))

        with pytest.raises(DistributionError, match="Unsafe archive member"):
            install_distribution(str(malicious), name="evil")

    def test_user_owned_exclude_covers_credentials(self):
        assert "auth.json" in USER_OWNED_EXCLUDE
        assert ".env" in USER_OWNED_EXCLUDE
        assert "memories" in USER_OWNED_EXCLUDE
        assert "sessions" in USER_OWNED_EXCLUDE
        assert "local" in USER_OWNED_EXCLUDE
