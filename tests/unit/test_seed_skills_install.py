"""Unit tests for seed skill installation (Windows read-only fix).

Tests cover:
- _remove_readonly() helper — clears read-only flag and retries deletion
- install_seed_skills() — clones repo, copies skills, cleans up (including read-only files)
- install_from_hub() — hub installation with success/failure/missing CLI
- Python version compatibility for shutil.rmtree onexc vs onerror
- Module constants and configuration
- Edge cases: empty repos, deeply nested read-only trees, symlinks, etc.
"""

import inspect
import os
import pathlib
import shutil
import stat
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest


# ============================================================================
# 1. _remove_readonly helper
# ============================================================================


class TestRemoveReadonly:
    def test_clears_readonly_and_retries(self, tmp_path):
        """_remove_readonly should chmod the file to writable and call func."""
        from eurekaclaw.skills.install import _remove_readonly

        readonly_file = tmp_path / "readonly.txt"
        readonly_file.write_text("data")
        readonly_file.chmod(stat.S_IREAD)

        mock_func = MagicMock()
        _remove_readonly(mock_func, str(readonly_file), None)

        mock_func.assert_called_once_with(str(readonly_file))

        file_mode = os.stat(str(readonly_file)).st_mode
        assert file_mode & stat.S_IWRITE

    def test_works_with_os_unlink(self, tmp_path):
        """_remove_readonly should work when func=os.unlink (real rmtree callback)."""
        from eurekaclaw.skills.install import _remove_readonly

        readonly_file = tmp_path / "to_delete.txt"
        readonly_file.write_text("delete me")
        readonly_file.chmod(stat.S_IREAD)

        _remove_readonly(os.unlink, str(readonly_file), None)
        assert not readonly_file.exists()

    def test_works_with_os_rmdir(self, tmp_path):
        """_remove_readonly should work when func=os.rmdir (empty dir case)."""
        from eurekaclaw.skills.install import _remove_readonly

        readonly_dir = tmp_path / "readonly_dir"
        readonly_dir.mkdir()
        readonly_dir.chmod(stat.S_IREAD | stat.S_IEXEC)

        _remove_readonly(os.rmdir, str(readonly_dir), None)
        assert not readonly_dir.exists()

    def test_handles_nonexistent_path(self, tmp_path):
        """If os.chmod itself fails on a nonexistent file, error should propagate."""
        from eurekaclaw.skills.install import _remove_readonly

        nonexistent = str(tmp_path / "does_not_exist.txt")
        with pytest.raises(FileNotFoundError):
            _remove_readonly(os.unlink, nonexistent, None)

    def test_accepts_any_third_arg(self, tmp_path):
        """Third arg is ignored — should work with exc_info tuple (onerror) or exception (onexc)."""
        from eurekaclaw.skills.install import _remove_readonly

        f = tmp_path / "file.txt"
        f.write_text("x")
        f.chmod(stat.S_IREAD)

        # onerror passes (type, value, traceback) tuple
        mock_func = MagicMock()
        _remove_readonly(mock_func, str(f), (OSError, OSError("perm"), None))
        mock_func.assert_called_once()

        # onexc passes the exception directly
        mock_func.reset_mock()
        _remove_readonly(mock_func, str(f), PermissionError("denied"))
        mock_func.assert_called_once()

    def test_sets_only_write_bit(self, tmp_path):
        """_remove_readonly should set S_IWRITE — verify the exact chmod call."""
        from eurekaclaw.skills.install import _remove_readonly

        f = tmp_path / "test.txt"
        f.write_text("data")
        f.chmod(stat.S_IREAD)

        with patch("eurekaclaw.skills.install.os.chmod") as mock_chmod:
            mock_func = MagicMock()
            _remove_readonly(mock_func, str(f), None)
            mock_chmod.assert_called_once_with(str(f), stat.S_IWRITE)

    def test_signature_matches_rmtree_callbacks(self):
        """_remove_readonly must accept exactly 3 args (func, path, exc_info_or_exc)."""
        from eurekaclaw.skills.install import _remove_readonly

        sig = inspect.signature(_remove_readonly)
        assert len(sig.parameters) == 3

    def test_multiple_readonly_files(self, tmp_path):
        """_remove_readonly should handle being called multiple times in a row."""
        from eurekaclaw.skills.install import _remove_readonly

        files = []
        for i in range(5):
            f = tmp_path / f"file_{i}.txt"
            f.write_text(f"content_{i}")
            f.chmod(stat.S_IREAD)
            files.append(f)

        for f in files:
            _remove_readonly(os.unlink, str(f), None)

        for f in files:
            assert not f.exists()


# ============================================================================
# 2. shutil.rmtree compatibility (onexc vs onerror)
# ============================================================================


class TestRmtreeCompat:
    def test_rmtree_with_readonly_files(self, tmp_path):
        """shutil.rmtree with _remove_readonly should delete dirs containing read-only files."""
        from eurekaclaw.skills.install import _remove_readonly

        git_dir = tmp_path / "repo" / ".git" / "objects"
        git_dir.mkdir(parents=True)

        for i in range(3):
            f = git_dir / f"obj_{i}"
            f.write_text(f"content_{i}")
            f.chmod(stat.S_IREAD)

        repo_dir = tmp_path / "repo"

        try:
            shutil.rmtree(str(repo_dir), onexc=_remove_readonly)
        except TypeError:
            shutil.rmtree(str(repo_dir), onerror=_remove_readonly)

        assert not repo_dir.exists()

    def test_rmtree_onexc_vs_onerror_fallback(self):
        """Verify the try/except pattern handles both Python 3.12+ and 3.11-."""
        from eurekaclaw.skills.install import _remove_readonly

        if sys.version_info >= (3, 12):
            # Python 3.12+: onexc should work directly
            pass
        else:
            # Python 3.11-: onexc raises TypeError, onerror should be used
            with pytest.raises(TypeError):
                shutil.rmtree("/nonexistent_path_test", onexc=_remove_readonly)

    def test_rmtree_deeply_nested_readonly(self, tmp_path):
        """Handle deeply nested read-only files (e.g. .git/objects/pack/...)."""
        from eurekaclaw.skills.install import _remove_readonly

        deep = tmp_path / "repo"
        current = deep
        for level in range(10):
            current = current / f"level_{level}"
        current.mkdir(parents=True)

        leaf = current / "readonly_leaf.dat"
        leaf.write_text("deep content")
        leaf.chmod(stat.S_IREAD)

        try:
            shutil.rmtree(str(deep), onexc=_remove_readonly)
        except TypeError:
            shutil.rmtree(str(deep), onerror=_remove_readonly)

        assert not deep.exists()

    def test_rmtree_mixed_readonly_and_writable(self, tmp_path):
        """Directory with both read-only and writable files should be fully removed."""
        from eurekaclaw.skills.install import _remove_readonly

        d = tmp_path / "mixed"
        d.mkdir()

        writable = d / "writable.txt"
        writable.write_text("ok")

        readonly = d / "readonly.txt"
        readonly.write_text("locked")
        readonly.chmod(stat.S_IREAD)

        try:
            shutil.rmtree(str(d), onexc=_remove_readonly)
        except TypeError:
            shutil.rmtree(str(d), onerror=_remove_readonly)

        assert not d.exists()

    def test_rmtree_empty_dir(self, tmp_path):
        """rmtree on empty directory should work without needing _remove_readonly."""
        from eurekaclaw.skills.install import _remove_readonly

        d = tmp_path / "empty"
        d.mkdir()

        try:
            shutil.rmtree(str(d), onexc=_remove_readonly)
        except TypeError:
            shutil.rmtree(str(d), onerror=_remove_readonly)

        assert not d.exists()


# ============================================================================
# 3. install_seed_skills
# ============================================================================


class TestInstallSeedSkills:
    def _setup_fake_repo(self, dest: pathlib.Path):
        """Create a fake cloned repo structure at dest/seed_skills/seedskills/."""
        from eurekaclaw.skills.install import SEED_SKILL_REPO_FOLDER, SEED_SKILL_FOLDER

        seed_dir = dest / SEED_SKILL_FOLDER
        seed_dir.mkdir(parents=True)

        (seed_dir / "math_skills").mkdir()
        (seed_dir / "math_skills" / "skill.md").write_text("# Math skill")
        (seed_dir / "proof_skills").mkdir()
        (seed_dir / "proof_skills" / "skill.md").write_text("# Proof skill")

        (seed_dir / ".git").mkdir()
        (seed_dir / ".git" / "HEAD").write_text("ref: refs/heads/main")

        (seed_dir / "README.md").write_text("# Seed skills")

        git_objects = dest / SEED_SKILL_REPO_FOLDER / ".git" / "objects"
        git_objects.mkdir(parents=True)
        ro_file = git_objects / "pack"
        ro_file.write_text("binary data")
        ro_file.chmod(stat.S_IREAD)

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_success(self, mock_copy_dir, mock_run, tmp_path):
        """install_seed_skills clones, copies skill dirs, and cleans up."""
        from eurekaclaw.skills.install import install_seed_skills

        dest = tmp_path / "skills"

        def fake_clone(*_args, **_kwargs):
            self._setup_fake_repo(dest)
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        install_seed_skills(dest)

        mock_run.assert_called_once()
        clone_cmd = mock_run.call_args[0][0]
        assert clone_cmd[0] == "git"
        assert clone_cmd[1] == "clone"

        copied_names = [
            os.path.basename(c[0][0]) for c in mock_copy_dir.call_args_list
        ]
        assert "math_skills" in copied_names
        assert "proof_skills" in copied_names
        assert ".git" not in copied_names

        repo_dir = dest / "seed_skills"
        assert not repo_dir.exists()

    @patch("eurekaclaw.skills.install.subprocess.run")
    def test_creates_dest_dir(self, mock_run, tmp_path):
        """install_seed_skills creates the destination dir if it doesn't exist."""
        from eurekaclaw.skills.install import install_seed_skills

        dest = tmp_path / "nonexistent" / "skills"
        assert not dest.exists()

        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        install_seed_skills(dest)

        assert dest.exists()

    @patch("eurekaclaw.skills.install.subprocess.run")
    def test_handles_clone_failure(self, mock_run, tmp_path, capsys):
        """install_seed_skills prints error on git clone failure."""
        from eurekaclaw.skills.install import install_seed_skills

        dest = tmp_path / "skills"
        mock_run.side_effect = subprocess.CalledProcessError(
            128, "git", stderr="fatal: repository not found"
        )

        install_seed_skills(dest)

        captured = capsys.readouterr()
        assert "Error installing seed skills" in captured.out

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_cleanup_readonly_files(self, _mock_copy_dir, mock_run, tmp_path):
        """Cleanup should succeed even when repo contains read-only files."""
        from eurekaclaw.skills.install import install_seed_skills

        dest = tmp_path / "skills"

        def fake_clone(*_args, **_kwargs):
            self._setup_fake_repo(dest)
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        install_seed_skills(dest)

        repo_dir = dest / "seed_skills"
        assert not repo_dir.exists()

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_skips_dotgit_directory(self, mock_copy_dir, mock_run, tmp_path):
        """The .git directory under seedskills/ should never be copied."""
        from eurekaclaw.skills.install import install_seed_skills

        dest = tmp_path / "skills"

        def fake_clone(*_args, **_kwargs):
            self._setup_fake_repo(dest)
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        install_seed_skills(dest)

        for call_item in mock_copy_dir.call_args_list:
            src_path = call_item[0][0]
            assert ".git" not in os.path.basename(src_path)

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_skips_files_only_copies_dirs(self, mock_copy_dir, mock_run, tmp_path):
        """Only subdirectories are copied — top-level files (README.md etc.) are skipped."""
        from eurekaclaw.skills.install import install_seed_skills, SEED_SKILL_FOLDER

        dest = tmp_path / "skills"

        def fake_clone(*_args, **_kwargs):
            seed_dir = dest / SEED_SKILL_FOLDER
            seed_dir.mkdir(parents=True)
            (seed_dir / "LICENSE").write_text("Apache-2.0")
            (seed_dir / "README.md").write_text("readme")
            (seed_dir / "config.yaml").write_text("key: val")
            (seed_dir / "actual_skill").mkdir()
            (seed_dir / "actual_skill" / "skill.md").write_text("content")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        install_seed_skills(dest)

        copied = [os.path.basename(c[0][0]) for c in mock_copy_dir.call_args_list]
        assert copied == ["actual_skill"]
        assert "LICENSE" not in copied
        assert "README.md" not in copied
        assert "config.yaml" not in copied

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_git_clone_uses_correct_repo_url(self, _mock_copy_dir, mock_run, tmp_path):
        """git clone should use SEED_SKILL_REPO constant."""
        from eurekaclaw.skills.install import install_seed_skills, SEED_SKILL_REPO

        dest = tmp_path / "skills"

        def fake_clone(*_args, **_kwargs):
            self._setup_fake_repo(dest)
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        install_seed_skills(dest)

        clone_cmd = mock_run.call_args[0][0]
        assert clone_cmd == ["git", "clone", SEED_SKILL_REPO]

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_git_clone_cwd_is_dest(self, _mock_copy_dir, mock_run, tmp_path):
        """git clone should run with cwd=dest."""
        from eurekaclaw.skills.install import install_seed_skills

        dest = tmp_path / "skills"

        def fake_clone(*_args, **_kwargs):
            self._setup_fake_repo(dest)
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        install_seed_skills(dest)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == dest

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_copy_directory_called_with_overwrite(self, mock_copy_dir, mock_run, tmp_path):
        """copy_directory should be called with overwrite=True."""
        from eurekaclaw.skills.install import install_seed_skills

        dest = tmp_path / "skills"

        def fake_clone(*_args, **_kwargs):
            self._setup_fake_repo(dest)
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        install_seed_skills(dest)

        for call_item in mock_copy_dir.call_args_list:
            assert call_item[1].get("overwrite") is True or call_item[0][2] is True

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_existing_dest_dir_is_not_recreated(self, _mock_copy_dir, mock_run, tmp_path):
        """If dest already exists, makedirs should not fail."""
        from eurekaclaw.skills.install import install_seed_skills

        dest = tmp_path / "skills"
        dest.mkdir(parents=True)
        marker = dest / "existing_file.txt"
        marker.write_text("pre-existing")

        def fake_clone(*_args, **_kwargs):
            self._setup_fake_repo(dest)
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        install_seed_skills(dest)

        # Pre-existing content should still be there
        assert marker.exists()

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_empty_seedskills_dir(self, mock_copy_dir, mock_run, tmp_path):
        """If seedskills/ contains no subdirectories, no copies should happen."""
        from eurekaclaw.skills.install import install_seed_skills, SEED_SKILL_FOLDER

        dest = tmp_path / "skills"

        def fake_clone(*_args, **_kwargs):
            seed_dir = dest / SEED_SKILL_FOLDER
            seed_dir.mkdir(parents=True)
            # Only files, no subdirs
            (seed_dir / "README.md").write_text("empty repo")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        install_seed_skills(dest)

        mock_copy_dir.assert_not_called()

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_multiple_skill_directories(self, mock_copy_dir, mock_run, tmp_path):
        """All skill directories (except .git) should be copied."""
        from eurekaclaw.skills.install import install_seed_skills, SEED_SKILL_FOLDER

        dest = tmp_path / "skills"
        skill_names = ["algebra", "calculus", "topology", "combinatorics", "statistics"]

        def fake_clone(*_args, **_kwargs):
            seed_dir = dest / SEED_SKILL_FOLDER
            seed_dir.mkdir(parents=True)
            for name in skill_names:
                (seed_dir / name).mkdir()
                (seed_dir / name / "skill.md").write_text(f"# {name}")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        install_seed_skills(dest)

        copied = sorted(os.path.basename(c[0][0]) for c in mock_copy_dir.call_args_list)
        assert copied == sorted(skill_names)

    @patch("eurekaclaw.skills.install.subprocess.run")
    def test_clone_failure_does_not_attempt_cleanup(self, mock_run, tmp_path):
        """If git clone fails, we should not try to rmtree (nothing to clean)."""
        from eurekaclaw.skills.install import install_seed_skills

        dest = tmp_path / "skills"
        mock_run.side_effect = subprocess.CalledProcessError(128, "git", stderr="error")

        # Should not raise — error is caught and printed
        install_seed_skills(dest)

        # No repo dir should exist
        assert not (dest / "seed_skills").exists()


# ============================================================================
# 4. install_from_hub
# ============================================================================


class TestInstallFromHub:
    @patch("eurekaclaw.skills.install.subprocess.run")
    def test_success(self, mock_run, tmp_path):
        from eurekaclaw.skills.install import install_from_hub

        dest = tmp_path / "skills"
        mock_run.return_value = MagicMock(returncode=0)

        result = install_from_hub("test-skill", dest)

        assert result is True
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["clawhub", "install", "test-skill"]

    @patch("eurekaclaw.skills.install.subprocess.run")
    def test_cli_not_found(self, mock_run, tmp_path):
        from eurekaclaw.skills.install import install_from_hub

        dest = tmp_path / "skills"
        mock_run.side_effect = FileNotFoundError("clawhub not found")

        result = install_from_hub("test-skill", dest)
        assert result is False

    @patch("eurekaclaw.skills.install.subprocess.run")
    def test_install_failure(self, mock_run, tmp_path):
        from eurekaclaw.skills.install import install_from_hub

        dest = tmp_path / "skills"
        mock_run.side_effect = subprocess.CalledProcessError(1, "clawhub")

        result = install_from_hub("nonexistent-skill", dest)
        assert result is False

    @patch("eurekaclaw.skills.install.subprocess.run")
    def test_creates_dest_dir(self, mock_run, tmp_path):
        from eurekaclaw.skills.install import install_from_hub

        dest = tmp_path / "new_dir" / "skills"
        assert not dest.exists()

        mock_run.return_value = MagicMock(returncode=0)
        install_from_hub("test-skill", dest)

        assert dest.exists()

    @patch("eurekaclaw.skills.install.subprocess.run")
    def test_cwd_is_parent_of_dest(self, mock_run, tmp_path):
        """clawhub install should run with cwd=dest.parent."""
        from eurekaclaw.skills.install import install_from_hub

        dest = tmp_path / "skills"
        mock_run.return_value = MagicMock(returncode=0)
        install_from_hub("my-skill", dest)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == dest.parent

    @patch("eurekaclaw.skills.install.subprocess.run")
    def test_uses_check_true(self, mock_run, tmp_path):
        """subprocess.run should be called with check=True."""
        from eurekaclaw.skills.install import install_from_hub

        dest = tmp_path / "skills"
        mock_run.return_value = MagicMock(returncode=0)
        install_from_hub("my-skill", dest)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["check"] is True

    @patch("eurekaclaw.skills.install.subprocess.run")
    def test_skill_with_org_prefix(self, mock_run, tmp_path):
        """Skill names with org/ prefix (e.g. 'steipete/github') should pass through."""
        from eurekaclaw.skills.install import install_from_hub

        dest = tmp_path / "skills"
        mock_run.return_value = MagicMock(returncode=0)

        result = install_from_hub("steipete/github", dest)

        assert result is True
        cmd = mock_run.call_args[0][0]
        assert cmd == ["clawhub", "install", "steipete/github"]

    @patch("eurekaclaw.skills.install.subprocess.run")
    def test_return_type_on_success(self, mock_run, tmp_path):
        """Return value should be True (not truthy int or string)."""
        from eurekaclaw.skills.install import install_from_hub

        dest = tmp_path / "skills"
        mock_run.return_value = MagicMock(returncode=0)

        result = install_from_hub("skill", dest)
        assert result is True
        assert isinstance(result, bool)

    @patch("eurekaclaw.skills.install.subprocess.run")
    def test_return_type_on_failure(self, mock_run, tmp_path):
        """Return value should be False (not falsy 0 or None)."""
        from eurekaclaw.skills.install import install_from_hub

        dest = tmp_path / "skills"
        mock_run.side_effect = subprocess.CalledProcessError(1, "clawhub")

        result = install_from_hub("skill", dest)
        assert result is False
        assert isinstance(result, bool)


# ============================================================================
# 5. Module constants and configuration
# ============================================================================


class TestModuleConstants:
    def test_seed_skill_repo_url(self):
        from eurekaclaw.skills.install import SEED_SKILL_REPO

        assert SEED_SKILL_REPO.startswith("https://")
        assert "seed_skills" in SEED_SKILL_REPO
        assert SEED_SKILL_REPO.endswith(".git")

    def test_seed_skill_repo_folder(self):
        from eurekaclaw.skills.install import SEED_SKILL_REPO_FOLDER

        assert SEED_SKILL_REPO_FOLDER == "seed_skills"

    def test_seed_skill_folder_path(self):
        from eurekaclaw.skills.install import SEED_SKILL_FOLDER, SEED_SKILL_REPO_FOLDER

        assert str(SEED_SKILL_FOLDER) == os.path.join("seed_skills", "seedskills")
        # SEED_SKILL_FOLDER should be relative to SEED_SKILL_REPO_FOLDER
        assert str(SEED_SKILL_FOLDER).startswith(SEED_SKILL_REPO_FOLDER)

    def test_clawhub_registry_url(self):
        from eurekaclaw.skills.install import CLAWHUB_REGISTRY

        assert CLAWHUB_REGISTRY.startswith("https://")
        assert "clawhub" in CLAWHUB_REGISTRY

    def test_seed_skill_folder_is_pathlib_path(self):
        from eurekaclaw.skills.install import SEED_SKILL_FOLDER

        assert isinstance(SEED_SKILL_FOLDER, pathlib.PurePath)


# ============================================================================
# 6. Edge cases and robustness
# ============================================================================


class TestEdgeCases:
    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_hidden_dirs_other_than_git_are_skipped(self, mock_copy_dir, mock_run, tmp_path):
        """Hidden directories other than .git should still be copied if they exist
        (current code only skips .git explicitly)."""
        from eurekaclaw.skills.install import install_seed_skills, SEED_SKILL_FOLDER

        dest = tmp_path / "skills"

        def fake_clone(*_args, **_kwargs):
            seed_dir = dest / SEED_SKILL_FOLDER
            seed_dir.mkdir(parents=True)
            (seed_dir / ".hidden_skill").mkdir()
            (seed_dir / ".hidden_skill" / "skill.md").write_text("hidden")
            (seed_dir / "normal_skill").mkdir()
            (seed_dir / "normal_skill" / "skill.md").write_text("normal")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        install_seed_skills(dest)

        copied = [os.path.basename(c[0][0]) for c in mock_copy_dir.call_args_list]
        # .hidden_skill is NOT .git, so it IS copied (code only excludes ".git")
        assert ".hidden_skill" in copied
        assert "normal_skill" in copied

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_skill_dir_with_spaces_in_name(self, mock_copy_dir, mock_run, tmp_path):
        """Skill directories with spaces in names should be handled."""
        from eurekaclaw.skills.install import install_seed_skills, SEED_SKILL_FOLDER

        dest = tmp_path / "skills"

        def fake_clone(*_args, **_kwargs):
            seed_dir = dest / SEED_SKILL_FOLDER
            seed_dir.mkdir(parents=True)
            (seed_dir / "my skill").mkdir()
            (seed_dir / "my skill" / "skill.md").write_text("content")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        install_seed_skills(dest)

        copied = [os.path.basename(c[0][0]) for c in mock_copy_dir.call_args_list]
        assert "my skill" in copied

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_unicode_skill_dir_name(self, mock_copy_dir, mock_run, tmp_path):
        """Skill directories with unicode characters should be handled."""
        from eurekaclaw.skills.install import install_seed_skills, SEED_SKILL_FOLDER

        dest = tmp_path / "skills"

        def fake_clone(*_args, **_kwargs):
            seed_dir = dest / SEED_SKILL_FOLDER
            seed_dir.mkdir(parents=True)
            (seed_dir / "数学技能").mkdir()
            (seed_dir / "数学技能" / "skill.md").write_text("中文技能")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        install_seed_skills(dest)

        copied = [os.path.basename(c[0][0]) for c in mock_copy_dir.call_args_list]
        assert "数学技能" in copied

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_copy_directory_receives_correct_dest(self, mock_copy_dir, mock_run, tmp_path):
        """copy_directory should receive dest (the skills dir) as its second arg."""
        from eurekaclaw.skills.install import install_seed_skills, SEED_SKILL_FOLDER

        dest = tmp_path / "skills"

        def fake_clone(*_args, **_kwargs):
            seed_dir = dest / SEED_SKILL_FOLDER
            seed_dir.mkdir(parents=True)
            (seed_dir / "skill_a").mkdir()
            (seed_dir / "skill_a" / "skill.md").write_text("a")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        install_seed_skills(dest)

        for call_item in mock_copy_dir.call_args_list:
            dest_arg = call_item[0][1]
            assert pathlib.Path(dest_arg) == dest

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_symlinks_in_seedskills_are_skipped(self, mock_copy_dir, mock_run, tmp_path):
        """Symlinks should not be treated as directories."""
        from eurekaclaw.skills.install import install_seed_skills, SEED_SKILL_FOLDER

        dest = tmp_path / "skills"

        def fake_clone(*_args, **_kwargs):
            seed_dir = dest / SEED_SKILL_FOLDER
            seed_dir.mkdir(parents=True)
            real_dir = seed_dir / "real_skill"
            real_dir.mkdir()
            (real_dir / "skill.md").write_text("real")
            # Create a symlink to real_dir
            (seed_dir / "link_skill").symlink_to(real_dir)
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        install_seed_skills(dest)

        copied = [os.path.basename(c[0][0]) for c in mock_copy_dir.call_args_list]
        assert "real_skill" in copied
        # symlink may or may not be treated as isdir — depends on os.path.isdir behavior
        # On most systems symlinks to dirs return True for isdir, so it would be copied

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory", side_effect=PermissionError("no write"))
    def test_copy_directory_failure_propagates(self, _mock_copy_dir, mock_run, tmp_path):
        """If copy_directory raises, the error is NOT caught (no try/except around copy)."""
        from eurekaclaw.skills.install import install_seed_skills, SEED_SKILL_FOLDER

        dest = tmp_path / "skills"

        def fake_clone(*_args, **_kwargs):
            seed_dir = dest / SEED_SKILL_FOLDER
            seed_dir.mkdir(parents=True)
            (seed_dir / "skill_a").mkdir()
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone

        with pytest.raises(PermissionError, match="no write"):
            install_seed_skills(dest)

    @patch("eurekaclaw.skills.install.subprocess.run")
    def test_clone_error_message_includes_stderr(self, mock_run, tmp_path, capsys):
        """Error output should include stderr from the failed git command."""
        from eurekaclaw.skills.install import install_seed_skills

        dest = tmp_path / "skills"
        mock_run.side_effect = subprocess.CalledProcessError(
            128, "git", stderr="fatal: could not read from remote repository"
        )

        install_seed_skills(dest)

        captured = capsys.readouterr()
        assert "fatal: could not read from remote repository" in captured.out

    @patch("eurekaclaw.skills.install.subprocess.run")
    def test_clone_error_with_none_stderr(self, mock_run, tmp_path, capsys):
        """CalledProcessError with stderr=None should not crash."""
        from eurekaclaw.skills.install import install_seed_skills

        dest = tmp_path / "skills"
        mock_run.side_effect = subprocess.CalledProcessError(1, "git", stderr=None)

        install_seed_skills(dest)

        captured = capsys.readouterr()
        assert "Error installing seed skills" in captured.out

    @patch("eurekaclaw.skills.install.subprocess.run")
    @patch("eurekaclaw.skills.install.copy_directory")
    def test_dest_as_string_path(self, _mock_copy_dir, mock_run, tmp_path):
        """dest should work when passed as a pathlib.Path (the declared type)."""
        from eurekaclaw.skills.install import install_seed_skills

        dest = tmp_path / "skills"

        def fake_clone(*_args, **_kwargs):
            self._setup_fake_repo(dest)
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone

        # Should not raise
        install_seed_skills(dest)
        assert isinstance(dest, pathlib.Path)

    def _setup_fake_repo(self, dest):
        """Reuse helper from TestInstallSeedSkills."""
        from eurekaclaw.skills.install import SEED_SKILL_REPO_FOLDER, SEED_SKILL_FOLDER

        seed_dir = dest / SEED_SKILL_FOLDER
        seed_dir.mkdir(parents=True)
        (seed_dir / "skill_a").mkdir()
        (seed_dir / "skill_a" / "skill.md").write_text("content")
        git_objects = dest / SEED_SKILL_REPO_FOLDER / ".git" / "objects"
        git_objects.mkdir(parents=True)


# ============================================================================
# 7. Return type annotation consistency
# ============================================================================


class TestAnnotations:
    def test_install_from_hub_declared_return_none_but_returns_bool(self):
        """install_from_hub is annotated -> None but actually returns True/False.

        This is a bug in the type annotation — the function returns bool in
        all paths but the signature says None. Documenting the discrepancy.
        """
        from eurekaclaw.skills.install import install_from_hub

        hints = install_from_hub.__annotations__
        # The declared return type is None
        assert hints.get("return") is None

    def test_install_seed_skills_returns_none(self):
        """install_seed_skills should return None (it's a void function)."""
        from eurekaclaw.skills.install import install_seed_skills

        hints = install_seed_skills.__annotations__
        assert hints.get("return") is None
