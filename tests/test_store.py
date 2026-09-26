"""Tests for _store.py. Pure stdlib (unittest); POSIX-only.

Windows-only code (_windows_api, _windows_current_user_sid,
_restrict_acl_windows, windows_private_dacl) is intentionally not exercised
here -- it cannot run on this platform and is covered by the Windows CI job.
"""

import json
import os
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _store  # noqa: E402


class _FakeNtOs:
    """Proxy that reports os.name == "nt" to _store only.

    Patching the real `os.name` also changes which pathlib flavour Path()
    constructs (WindowsPath vs PosixPath), which breaks validate_store_path
    on a POSIX test runner. This proxy is swapped in for _store's own `os`
    reference so only _store's `os.name` checks see "nt"; pathlib (and
    everything else) keeps using the real, POSIX os module.
    """

    def __getattr__(self, item):
        if item == "name":
            return "nt"
        return getattr(os, item)


def patch_nt():
    return mock.patch.object(_store, "os", _FakeNtOs())


class ValidateStorePathTest(unittest.TestCase):
    def test_empty_path_rejected(self):
        with self.assertRaises(_store.StorePathError):
            _store.validate_store_path("")

    def test_none_rejected(self):
        with self.assertRaises(_store.StorePathError):
            _store.validate_store_path(None)

    def test_non_string_non_path_rejected(self):
        with self.assertRaises(_store.StorePathError):
            _store.validate_store_path(12345)

    def test_control_characters_rejected(self):
        with self.assertRaises(_store.StorePathError):
            _store.validate_store_path("/tmp/foo\x00bar")

    def test_relative_path_rejected(self):
        with self.assertRaises(_store.StorePathError):
            _store.validate_store_path("relative/path")

    def test_dotdot_segment_rejected(self):
        with self.assertRaises(_store.StorePathError):
            _store.validate_store_path("/tmp/foo/../bar")

    def test_dotdot_after_resolve_rejected(self):
        # A symlink whose target resolves outside via ".." after following links.
        tmp_dir = tempfile.mkdtemp()
        try:
            target_root = Path(tmp_dir) / "real"
            target_root.mkdir()
            outside = Path(tmp_dir) / "outside"
            outside.mkdir()
            link = target_root / "link"
            os.symlink(outside, link)
            candidate = target_root / "link" / ".." / "escaped"
            # This still contains a literal ".." segment in candidate.parts,
            # so it's rejected by the pre-resolve check already. Confirm it
            # raises regardless of which check catches it.
            with self.assertRaises(_store.StorePathError):
                _store.validate_store_path(candidate)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_valid_absolute_path_accepted(self):
        tmp_dir = tempfile.mkdtemp()
        try:
            candidate = Path(tmp_dir) / "sub" / "file.json"
            resolved = _store.validate_store_path(candidate)
            self.assertTrue(resolved.is_absolute())
            self.assertEqual(resolved, candidate.resolve(strict=False))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_accepts_path_instance(self):
        tmp_dir = tempfile.mkdtemp()
        try:
            resolved = _store.validate_store_path(Path(tmp_dir) / "x")
            self.assertTrue(resolved.is_absolute())
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_purpose_in_error_message(self):
        with self.assertRaises(_store.StorePathError) as ctx:
            _store.validate_store_path("", purpose="lock")
        self.assertIn("lock", str(ctx.exception))


class WarnOnceTest(unittest.TestCase):
    def setUp(self):
        # Isolate from other tests / modules that may have already warned.
        self._saved = set(_store._WARNED_PATHS)
        _store._WARNED_PATHS.clear()

    def tearDown(self):
        _store._WARNED_PATHS.clear()
        _store._WARNED_PATHS.update(self._saved)

    def test_dedup_second_call_suppressed(self):
        calls = []
        _store._warn_once("msg", "/tmp/a", logger=calls.append)
        _store._warn_once("msg", "/tmp/a", logger=calls.append)
        self.assertEqual(len(calls), 1)

    def test_different_path_not_suppressed(self):
        calls = []
        _store._warn_once("msg", "/tmp/a", logger=calls.append)
        _store._warn_once("msg", "/tmp/b", logger=calls.append)
        self.assertEqual(len(calls), 2)

    def test_uses_logger_when_given(self):
        calls = []
        _store._warn_once("hello", "/tmp/c", logger=calls.append)
        self.assertEqual(len(calls), 1)
        self.assertIn("hello", calls[0])
        self.assertIn("/tmp/c", calls[0])

    def test_falls_back_to_warnings_warn(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _store._warn_once("no-logger-msg", "/tmp/d")
            self.assertEqual(len(caught), 1)
            self.assertIn("no-logger-msg", str(caught[0].message))


class RestrictPrivatePathTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @unittest.skipIf(os.name == "nt", "POSIX chmod semantics only")
    def test_restricts_file_to_0600(self):
        file_path = Path(self.tmp_dir) / "secret.txt"
        file_path.write_text("data")
        os.chmod(file_path, 0o644)
        _store.restrict_private_path(file_path)
        mode = stat.S_IMODE(file_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX chmod semantics only")
    def test_restricts_dir_to_0700(self):
        dir_path = Path(self.tmp_dir) / "subdir"
        dir_path.mkdir()
        os.chmod(dir_path, 0o755)
        _store.restrict_private_path(dir_path)
        mode = stat.S_IMODE(dir_path.stat().st_mode)
        self.assertEqual(mode, 0o700)

    @unittest.skipIf(os.name == "nt", "exercises the nt-branch selection from POSIX")
    def test_windows_branch_selection_falls_back_on_acl_failure(self):
        # We cannot exercise the real Win32 ACL calls on this platform, but
        # we can confirm restrict_private_path's os.name=="nt" branch is
        # taken and that a failure inside it is swallowed (warn + False)
        # rather than propagating and corrupting the write. ctypes.WinDLL
        # does not exist off Windows, so _restrict_acl_windows fails
        # naturally without any further mocking.
        file_path = Path(self.tmp_dir) / "secret.txt"
        file_path.write_text("data")
        calls = []
        with patch_nt():
            result = _store.restrict_private_path(file_path, logger=calls.append)
        self.assertFalse(result)
        self.assertEqual(len(calls), 1)
        self.assertIn("could not restrict Windows ACL", calls[0])


class ExistingDirectoryWarningTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._saved = set(_store._WARNED_PATHS)
        _store._WARNED_PATHS.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        _store._WARNED_PATHS.clear()
        _store._WARNED_PATHS.update(self._saved)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits only")
    def test_wide_perms_trigger_warning(self):
        dir_path = Path(self.tmp_dir) / "wide"
        dir_path.mkdir()
        os.chmod(dir_path, 0o777)
        calls = []
        _store._existing_directory_warning(dir_path, logger=calls.append)
        self.assertEqual(len(calls), 1)
        self.assertIn("mode", calls[0])

    @unittest.skipIf(os.name == "nt", "POSIX mode bits only")
    def test_private_perms_no_warning(self):
        dir_path = Path(self.tmp_dir) / "private"
        dir_path.mkdir()
        os.chmod(dir_path, 0o700)
        calls = []
        _store._existing_directory_warning(dir_path, logger=calls.append)
        self.assertEqual(len(calls), 0)

    def test_missing_path_no_warning_no_raise(self):
        calls = []
        _store._existing_directory_warning(Path(self.tmp_dir) / "does-not-exist", logger=calls.append)
        self.assertEqual(len(calls), 0)

    def test_nt_short_circuits_before_stat(self):
        # On Windows this function is a documented no-op (DACLs, not mode
        # bits, govern access there). Confirm the branch returns immediately.
        dir_path = Path(self.tmp_dir) / "any"
        dir_path.mkdir()
        calls = []
        with patch_nt():
            _store._existing_directory_warning(dir_path, logger=calls.append)
        self.assertEqual(len(calls), 0)


class EnsureParentTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._saved = set(_store._WARNED_PATHS)
        _store._WARNED_PATHS.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        _store._WARNED_PATHS.clear()
        _store._WARNED_PATHS.update(self._saved)

    def test_creates_nested_missing_dirs_private(self):
        target = Path(self.tmp_dir) / "a" / "b" / "c" / "file.json"
        safe = _store.ensure_parent(target, private=True)
        self.assertEqual(safe, target.resolve(strict=False))
        for sub in ("a", "a/b", "a/b/c"):
            d = Path(self.tmp_dir) / sub
            self.assertTrue(d.is_dir())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits only")
    def test_existing_dir_wide_perms_warns(self):
        target = Path(self.tmp_dir) / "file.json"
        # Parent (tmp_dir) already exists -- make it wide open.
        os.chmod(self.tmp_dir, 0o777)
        calls = []
        _store.ensure_parent(target, private=True, logger=calls.append)
        self.assertEqual(len(calls), 1)
        self.assertIn("mode", calls[0])

    def test_file_exists_error_race_is_caught(self):
        target = Path(self.tmp_dir) / "racedir" / "file.json"
        parent = target.parent
        original_mkdir = os.mkdir

        def flaky_mkdir(path, mode):
            # Simulate another process winning the race to create the dir.
            original_mkdir(path, mode)
            raise FileExistsError(str(path))

        with mock.patch.object(os, "mkdir", side_effect=flaky_mkdir):
            safe = _store.ensure_parent(target, private=True)
        self.assertEqual(safe, target.resolve(strict=False))
        self.assertTrue(parent.is_dir())

    def test_ensure_private_dir_compat_helper(self):
        target = Path(self.tmp_dir) / "compat" / "file.json"
        safe = _store.ensure_private_dir(target)
        self.assertEqual(safe, target.resolve(strict=False))
        self.assertTrue((Path(self.tmp_dir) / "compat").is_dir())


class FileLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.target = Path(self.tmp_dir) / "store.json"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @mock.patch.object(_store, "_NOFOLLOW_DIRS", False)  # the Windows lock-file protocol
    def test_basic_acquire_release(self):
        lock_path = Path(str(self.target) + ".lock")
        with _store.FileLock(self.target):
            self.assertTrue(lock_path.exists())
        self.assertFalse(lock_path.exists())

    @mock.patch.object(_store, "_NOFOLLOW_DIRS", False)  # the Windows lock-file protocol
    def test_stale_lock_is_recovered(self):
        lock_path = Path(str(self.target) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("99999")
        old_time = time.time() - (_store.LOCK_STALE_SECONDS + 10)
        os.utime(lock_path, (old_time, old_time))

        with _store.FileLock(self.target, timeout_seconds=2, retry_interval=0.01):
            # New lock content should belong to this process now.
            self.assertEqual(lock_path.read_text(), str(os.getpid()))
        self.assertFalse(lock_path.exists())

    @mock.patch.object(_store, "_NOFOLLOW_DIRS", False)  # the Windows lock-file protocol
    def test_timeout_when_lock_held_and_fresh(self):
        lock_path = Path(str(self.target) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("12345")
        # Fresh mtime (just written) -- not stale.
        with self.assertRaises(TimeoutError):
            with _store.FileLock(self.target, timeout_seconds=0.2, retry_interval=0.05):
                pass
        lock_path.unlink(missing_ok=True)

    @unittest.skipIf(os.name == "nt", "exercises the nt-branch selection from POSIX")
    def test_nt_branch_calls_restrict_private_path_on_acquire(self):
        # On Windows, FileLock also restricts the lock file's ACL right
        # after creating it. We can't run the real Win32 call here, but we
        # can confirm the branch is taken (and that its internal failure is
        # swallowed rather than breaking lock acquisition).
        with patch_nt():
            with _store.FileLock(self.target, timeout_seconds=1):
                pass

    def test_reentrant_style_sequential_use(self):
        with _store.FileLock(self.target, timeout_seconds=1):
            pass
        with _store.FileLock(self.target, timeout_seconds=1):
            pass

    @mock.patch.object(_store, "_NOFOLLOW_DIRS", False)  # the Windows/fallback path
    def test_getmtime_race_falls_back_to_timeout(self):
        # Simulate: os.open always contends (FileExistsError), and the
        # lock file vanishes out from under getmtime (another process wins
        # the stale-lock cleanup race) -- exercises the "except OSError"
        # branch around the getmtime call, not just the outer except.
        lock_path = Path(str(self.target) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        with (
            mock.patch("os.open", side_effect=FileExistsError()),
            mock.patch("os.path.getmtime", side_effect=OSError("vanished")),
        ):
            with self.assertRaises(TimeoutError):
                with _store.FileLock(self.target, timeout_seconds=0.15, retry_interval=0.05):
                    pass

    @mock.patch.object(_store, "_NOFOLLOW_DIRS", False)  # the Windows/fallback path
    def test_getmtime_race_then_recovers(self):
        # First contention hits the getmtime-OSError branch (file removed
        # concurrently), then a later attempt succeeds normally.
        lock_path = Path(str(self.target) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        real_open = os.open
        calls = {"n": 0}

        def flaky_open(path, flags, mode=0o777):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FileExistsError()
            return real_open(path, flags, mode)

        with (
            mock.patch("os.open", side_effect=flaky_open),
            mock.patch("os.path.getmtime", side_effect=OSError("vanished")),
        ):
            with _store.FileLock(self.target, timeout_seconds=2, retry_interval=0.01):
                pass
        self.assertGreaterEqual(calls["n"], 2)


class UniqueTmpPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_returns_path_in_same_dir(self):
        # unique_tmp_path resolves symlinks (e.g. macOS /var -> /private/var),
        # so compare against the resolved parent, not the raw input parent.
        safe = Path(self.tmp_dir) / "file.json"
        tmp = _store.unique_tmp_path(safe)
        self.assertEqual(tmp.parent, safe.resolve(strict=False).parent)

    def test_different_names_on_repeated_calls(self):
        safe = Path(self.tmp_dir) / "file.json"
        tmp1 = _store.unique_tmp_path(safe)
        tmp2 = _store.unique_tmp_path(safe)
        self.assertNotEqual(tmp1, tmp2)


class AtomicReplaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_replaces_file_atomically(self):
        target = Path(self.tmp_dir) / "file.txt"
        target.write_text("old")
        tmp = Path(self.tmp_dir) / "file.txt.tmp"
        tmp.write_text("new")
        _store.atomic_replace(tmp, target)
        self.assertEqual(target.read_text(), "new")
        self.assertFalse(tmp.exists())

    def test_retries_on_transient_permission_error(self):
        target = Path(self.tmp_dir) / "file.txt"
        target.write_text("old")
        tmp = Path(self.tmp_dir) / "file.txt.tmp"
        tmp.write_text("new")

        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("transient")
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=flaky_replace), mock.patch("time.sleep", return_value=None):
            _store.atomic_replace(tmp, target)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(target.read_text(), "new")

    def test_raises_after_exhausting_retries(self):
        target = Path(self.tmp_dir) / "file.txt"
        target.write_text("old")
        tmp = Path(self.tmp_dir) / "file.txt.tmp"
        tmp.write_text("new")

        with (
            mock.patch("os.replace", side_effect=PermissionError("stuck")),
            mock.patch("time.sleep", return_value=None),
        ):
            with self.assertRaises(PermissionError):
                _store.atomic_replace(tmp, target)


class AtomicWriteTextTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_writes_text_with_private_perms(self):
        target = Path(self.tmp_dir) / "out.txt"
        _store.atomic_write_text(target, "hello world")
        self.assertEqual(target.read_text(), "hello world")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits only")
    def test_non_private_preserves_existing_perms(self):
        target = Path(self.tmp_dir) / "shared.txt"
        target.write_text("initial")
        os.chmod(target, 0o644)
        _store.atomic_write_text(target, "updated", private=False)
        self.assertEqual(target.read_text(), "updated")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_non_private_new_file_uses_default_mode(self):
        target = Path(self.tmp_dir) / "new_shared.txt"
        _store.atomic_write_text(target, "content", private=False)
        self.assertEqual(target.read_text(), "content")

    def test_fd_closed_and_tmp_removed_when_fdopen_fails(self):
        # If os.fdopen raises before `fd = None` runs, the finally block
        # must still close the raw fd and clean up the tmp file.
        target = Path(self.tmp_dir) / "out.txt"
        real_open, real_close = os.open, os.close
        opened, closed = [], []

        def tracking_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def tracking_close(fd):
            closed.append(fd)
            return real_close(fd)

        with (
            mock.patch("os.fdopen", side_effect=OSError("boom")),
            mock.patch("os.open", side_effect=tracking_open),
            mock.patch("os.close", side_effect=tracking_close),
        ):
            with self.assertRaises(OSError):
                _store.atomic_write_text(target, "hello")
        self.assertTrue(opened, "atomic_write_text never opened a temp file")
        self.assertEqual(sorted(closed), sorted(opened), "raw fd leaked after os.fdopen failed")
        self.assertFalse(target.exists())
        leftovers = list(Path(self.tmp_dir).glob(".out.txt.*.tmp"))
        self.assertEqual(leftovers, [])


class JsonRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_atomic_write_json_then_load(self):
        target = Path(self.tmp_dir) / "data.json"
        _store.atomic_write_json(target, {"a": 1, "b": [1, 2, 3]})
        loaded = _store.load_json_dict(target)
        self.assertEqual(loaded, {"a": 1, "b": [1, 2, 3]})

    def test_load_json_list_round_trip(self):
        target = Path(self.tmp_dir) / "list.json"
        _store.save_json_list(target, [1, 2, 3])
        self.assertEqual(_store.load_json_list(target), [1, 2, 3])

    def test_load_json_dict_round_trip(self):
        target = Path(self.tmp_dir) / "dict.json"
        _store.save_json_dict(target, {"x": "y"})
        self.assertEqual(_store.load_json_dict(target), {"x": "y"})

    def test_load_json_list_missing_file_returns_empty(self):
        target = Path(self.tmp_dir) / "missing.json"
        self.assertEqual(_store.load_json_list(target), [])

    def test_load_json_dict_missing_file_returns_empty(self):
        target = Path(self.tmp_dir) / "missing.json"
        self.assertEqual(_store.load_json_dict(target), {})

    def test_bad_json_returns_empty_default(self):
        target = Path(self.tmp_dir) / "bad.json"
        target.write_text("{not valid json")
        calls = []
        self.assertEqual(_store.load_json_dict(target, logger=calls.append), {})
        self.assertEqual(len(calls), 1)

    def test_wrong_type_returns_empty_default(self):
        # File contains a valid JSON list, but load_json_dict expects a dict.
        target = Path(self.tmp_dir) / "wrong_type.json"
        target.write_text(json.dumps([1, 2, 3]))
        self.assertEqual(_store.load_json_dict(target), {})

        target2 = Path(self.tmp_dir) / "wrong_type2.json"
        target2.write_text(json.dumps({"a": 1}))
        self.assertEqual(_store.load_json_list(target2), [])

    def test_bad_path_raises_store_path_error_is_swallowed(self):
        # validate_store_path rejects relative paths; _load_json catches
        # StorePathError internally and returns the empty default.
        calls = []
        result = _store._load_json("relative/path.json", dict, {}, logger=calls.append)
        self.assertEqual(result, {})
        self.assertEqual(len(calls), 1)
        self.assertIn("refused", calls[0])


@unittest.skipUnless(_store._NOFOLLOW_DIRS, "POSIX directory-relative operations")
class NoFollowStoreTest(unittest.TestCase):
    """Codex Security scan findings 3 and 5: store reads, writes and locks must
    not follow a symlink planted at the file or swapped in for a parent."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.store_dir = self.root / "store"
        self.store_dir.mkdir()
        self.target = self.store_dir / "learned.json"
        self.victim = self.root / "victim.json"
        self.victim.write_text('["victim"]')

    # Covers SECURITY.md#filesystem-stores-and-publication-outputs
    def test_symlinked_store_file_is_not_read_through(self):
        os.symlink(self.victim, self.target)
        self.assertEqual(_store.load_json_list(self.target, logger=lambda m: None), [])

    def test_read_private_text_refuses_symlinks_and_reports_missing(self):
        self.assertIsNone(_store.read_private_text(self.target))
        self.assertIsNone(_store.read_private_text(self.root / "missing-dir" / "x.json"))
        os.symlink(self.victim, self.target)
        with self.assertRaises(_store.StoreRedirectError):
            _store.read_private_text(self.target)

    def test_ai_finops_read_modify_write_holds_the_store_file_lock(self):
        import ai_finops

        taken = []
        real = _store.FileLock

        class Recording(real):
            def __enter__(self):
                taken.append(self.lock_path)
                return super().__enter__()

        business = self.store_dir / "business.json"
        source = self.root / "features.json"
        source.write_text("[]")
        decisions = self.store_dir / "decisions.json"
        with (
            mock.patch.object(_store, "FileLock", Recording),
            mock.patch.object(ai_finops, "_decision_store_path", decisions),
        ):
            ai_finops.import_business_data("feature", source, store_path=business, emit_otlp=False)
            with (
                mock.patch.object(ai_finops, "_pseudonymize", return_value="h"),
                mock.patch.object(ai_finops, "emit_otlp_records", return_value=False),
            ):
                text, is_error = ai_finops.record_recommendation_decision("rec_" + "a" * 16, "approved", "owner", "why")
                self.assertFalse(is_error, text)
        self.assertIn(str(business) + ".lock", taken)
        self.assertIn(str(decisions) + ".lock", taken)

    def test_ai_finops_private_stores_do_not_follow_symlinks(self):
        import ai_finops

        attacker_key = self.root / "attacker.key"
        attacker_key.write_text("ab" * 32 + "\n")
        key_path = self.store_dir / "pseudonym.key"
        os.symlink(attacker_key, key_path)
        saved = (ai_finops._pseudonym_key_cache, ai_finops._pseudonym_key_source)
        try:
            ai_finops._pseudonym_key_cache = ai_finops._pseudonym_key_source = None
            with mock.patch.dict(os.environ), mock.patch.object(ai_finops, "_pseudonym_key_path", key_path):
                os.environ.pop("BERSERK_MCP_PSEUDONYM_KEY", None)
                with self.assertRaises(OSError):
                    ai_finops._deployment_pseudonym_key()
        finally:
            ai_finops._pseudonym_key_cache, ai_finops._pseudonym_key_source = saved
        business = self.store_dir / "business.json"
        os.symlink(self.victim, business)
        self.assertEqual(ai_finops.load_business_store(business), ai_finops._empty_business_store())

    def test_symlinked_store_file_is_not_written_through(self):
        os.symlink(self.victim, self.target)
        with self.assertRaises(_store.StoreRedirectError):
            _store._atomic_write_nofollow(self.target, "[]", True)
        self.assertEqual(self.victim.read_text(), '["victim"]')

    def test_parent_swapped_for_symlink_after_validation_is_refused(self):
        safe = _store.ensure_parent(self.target, private=True)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.store_dir.rename(self.root / "moved")
        os.symlink(elsewhere, self.store_dir)  # the swap happens after validation
        with self.assertRaises(_store.StoreRedirectError):
            _store._atomic_write_nofollow(safe, "[1]", True)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_write_is_atomic_private_and_leaves_no_temp_file(self):
        _store.atomic_write_json(self.target, [1, 2])
        self.assertEqual(json.loads(self.target.read_text()), [1, 2])
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.store_dir.iterdir()), ["learned.json"])

    def test_public_write_keeps_the_existing_mode(self):
        self.target.write_text("x")
        os.chmod(self.target, 0o644)
        _store.atomic_write_text(self.target, "y", private=False)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o644)
        self.assertEqual(self.target.read_text(), "y")

    def test_lock_is_not_taken_through_a_symlink(self):
        os.symlink(self.victim, str(self.target) + ".lock")
        with self.assertRaises((_store.StoreRedirectError, TimeoutError)):
            with _store.FileLock(self.target, timeout_seconds=0.1, retry_interval=0.02):
                pass
        self.assertEqual(self.victim.read_text(), '["victim"]')
        self.assertTrue(self.victim.exists())

    def test_posix_lock_is_a_kernel_lock_on_a_kept_file(self):
        lock_path = Path(str(self.target) + ".lock")
        with _store.FileLock(self.target, timeout_seconds=1):
            self.assertTrue(lock_path.exists())
        self.assertTrue(lock_path.exists())  # kept: removing it would reopen the unlink race
        with _store.FileLock(self.target, timeout_seconds=1):
            pass

    def test_leftover_lock_file_does_not_block(self):
        lock_path = Path(str(self.target) + ".lock")
        lock_path.write_text("99999")  # e.g. from the old lock-file protocol or a crash
        with _store.FileLock(self.target, timeout_seconds=0.5, retry_interval=0.02):
            pass

    def _hold_lock_in_child(self, seconds):
        code = (
            "import sys, time; sys.path.insert(0, sys.argv[1]); import _store\n"
            "with _store.FileLock(sys.argv[2], timeout_seconds=5):\n"
            "    print('held', flush=True); time.sleep(float(sys.argv[3]))\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(Path(_store.__file__).parent), str(self.target), str(seconds)],
            stdout=subprocess.PIPE,
            text=True,
        )
        ready, _, _ = select.select([child.stdout], [], [], 20)
        if not ready:
            child.kill()
            child.wait()
            child.stdout.close()
            self.fail("lock-holding child did not start within 20s")
        self.assertEqual(child.stdout.readline().strip(), "held")
        return child

    def test_lock_held_by_another_process_times_out(self):
        child = self._hold_lock_in_child(30)
        try:
            with self.assertRaises(TimeoutError):
                with _store.FileLock(self.target, timeout_seconds=0.2, retry_interval=0.02):
                    pass
        finally:
            child.kill()
            child.wait()
            child.stdout.close()

    def test_lock_of_a_crashed_process_is_released_by_the_kernel(self):
        child = self._hold_lock_in_child(30)
        child.kill()  # no cleanup runs, as in a crash
        child.wait()
        child.stdout.close()
        with _store.FileLock(self.target, timeout_seconds=0.5, retry_interval=0.02):
            pass

    def test_threads_contending_for_the_lock_lose_no_update(self):
        # Caught a macOS kernel race (create-open via a directory fd failing
        # with ENOENT under contention) that lost about 1 update in 8 rounds.
        for round_no in range(8):
            target = self.store_dir / f"q{round_no}.json"

            def worker(i, target=target):
                with _store.FileLock(target):
                    items = _store.load_json_list(target)
                    items.append(i)
                    _store.save_json_list(target, items)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
            self.assertEqual(sorted(_store.load_json_list(target)), list(range(20)))

    def test_create_open_retries_a_transient_enoent_only(self):
        dir_fd = os.open(self.store_dir, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, dir_fd)
        real_open = os.open
        calls = {"n": 0}

        def once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FileNotFoundError(2, "transient")
            return real_open(*args, **kwargs)

        with mock.patch.object(_store.os, "open", side_effect=once):
            os.close(_store._open_file_nofollow(dir_fd, "a.lock", os.O_CREAT | os.O_RDWR))
        self.assertEqual(calls["n"], 2)
        with self.assertRaises(FileNotFoundError):  # a plain open of a missing file is not retried
            _store._open_file_nofollow(dir_fd, "missing.json", os.O_RDONLY)
        with mock.patch.object(_store.os, "open", side_effect=FileNotFoundError(2, "gone")) as always:
            with self.assertRaises(FileNotFoundError):
                _store._open_file_nofollow(dir_fd, "b.lock", os.O_CREAT | os.O_RDWR)
        self.assertEqual(always.call_count, _store._CREATE_ENOENT_RETRIES + 1)

    def test_lock_failure_leaks_no_descriptor(self):
        before = len(os.listdir("/dev/fd"))
        with mock.patch.object(_store.fcntl, "flock", side_effect=OSError("boom")):
            for _ in range(20):
                with self.assertRaises(OSError):
                    with _store.FileLock(self.target, timeout_seconds=0.1):
                        pass
        self.assertLessEqual(len(os.listdir("/dev/fd")), before)


if __name__ == "__main__":
    unittest.main()
