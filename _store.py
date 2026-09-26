"""Shared, stdlib-only filesystem primitives for berserk-mcp.

Private stores are written atomically and restricted to the current user.
On POSIX this means 0600 files and 0700 directories created by this module.
On Windows a protected DACL grants full control only to the current user.
Existing directories are never chmod'd or have their DACL replaced: operators
may intentionally share a publication directory with a BI service account.
"""

import json
import os
import secrets
import stat
import threading
import time
import warnings
from pathlib import Path
import contextlib

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]


LOCK_STALE_SECONDS = 30
LOCK_TIMEOUT_SECONDS = 10
LOCK_RETRY_INTERVAL = 0.05

_WARNED_PATHS = set()
_WARN_LOCK = threading.Lock()


class StorePathError(ValueError):
    """Raised when a caller-supplied filesystem path is unsafe."""


def validate_store_path(candidate, purpose="store"):
    """Return an absolute path whose parent is resolved, after rejecting
    traversal and controls.

    The last component is kept as given, not resolved: a symlink planted at a
    store file must be refused by the no-follow operations below rather than
    silently followed (Codex Security scan, finding 5)."""
    if not candidate:
        raise StorePathError(f"{purpose} path is empty")
    if not isinstance(candidate, (str, Path)):
        raise StorePathError(f"{purpose} path must be a string or Path")
    text = str(candidate)
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise StorePathError(f"{purpose} path contains control characters")
    candidate_path = Path(text)
    if not candidate_path.is_absolute():
        raise StorePathError(f"{purpose} path must be absolute (got {text!r})")
    if ".." in candidate_path.parts:
        raise StorePathError(f"{purpose} path must not contain '..' segments")
    resolved = candidate_path.parent.resolve(strict=False) / candidate_path.name
    if ".." in resolved.parts:
        raise StorePathError(f"{purpose} path resolves through '..'")
    return resolved


def _warn_once(message, path, logger=None):
    key = (message, str(path))
    with _WARN_LOCK:
        if key in _WARNED_PATHS:
            return
        _WARNED_PATHS.add(key)
    rendered = f"{message}: {path}"
    if logger:
        logger(rendered)
    else:
        warnings.warn(rendered, RuntimeWarning, stacklevel=3)


# Symlink- and race-safe file operations (Codex Security scan, findings 3 and
# 5). validate_store_path resolves a path once, but every later operation used
# to look the path up again, so a parent directory swapped for a symlink in
# between -- or a symlink planted at the file itself -- redirected reads and
# writes. On POSIX each operation now opens the parent directory component by
# component without following symlinks, then works relative to that open
# directory with O_NOFOLLOW on the file. Windows has no directory-relative
# calls in Python; there the per-user DACL is what keeps other users out.
_NOFOLLOW_DIRS = (
    os.name != "nt"
    and fcntl is not None
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
)


class StoreRedirectError(OSError):
    """A store path contains a symlink or non-directory that was not there when
    it was validated, or the store file itself is a symlink."""


def _open_dir_nofollow(directory):
    """File descriptor for the absolute `directory`, opened one component at a
    time without following symlinks. Raises StoreRedirectError when any
    component is a symlink or not a directory."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(directory).parts[1:]:
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                if isinstance(exc, FileNotFoundError):
                    raise
                raise StoreRedirectError(
                    exc.errno, f"refusing store directory component {part!r}: {exc.strerror}"
                ) from None
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    return fd


# macOS can fail a create-open relative to a directory descriptor with ENOENT
# while another thread creates the same name (measured: about 1 in 9 opens
# with 16 threads; a path-based open never does). One retry always succeeded
# in 6,400 trials; the bound keeps a directory that is really gone an error.
_CREATE_ENOENT_RETRIES = 20


def _open_file_nofollow(dir_fd, name, flags, mode=0o600):
    for attempt in range(_CREATE_ENOENT_RETRIES + 1):
        try:
            return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
        except FileNotFoundError:
            if not flags & os.O_CREAT or attempt == _CREATE_ENOENT_RETRIES:
                raise
            time.sleep(0.0005)
        except OSError as exc:
            if isinstance(exc, FileExistsError):
                raise
            if stat.S_ISLNK(_lstat_at(dir_fd, name, default=0)):
                raise StoreRedirectError(exc.errno, f"refusing symlinked store file {name!r}") from None
            raise
    raise AssertionError("unreachable")  # pragma: no cover


def _lstat_at(dir_fd, name, default=None):
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
    except OSError:
        return default


def _windows_api():
    """Return configured Windows security DLL handles and ctypes types."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return ctypes, wintypes, advapi32, kernel32


def _windows_current_user_sid():
    ctypes, wintypes, advapi32, kernel32 = _windows_api()

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", SID_AND_ATTRIBUTES)]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, needed.value, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        token_user = ctypes.cast(buffer, ctypes.POINTER(TOKEN_USER)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(token_user.User.Sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return sid_text.value
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def _restrict_acl_windows(path):
    """Apply a protected current-user-only DACL to a file or directory."""
    ctypes, wintypes, advapi32, kernel32 = _windows_api()
    sid = _windows_current_user_sid()
    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    sddl = f"D:P(A;;FA;;;{sid})"
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), ctypes.byref(descriptor_size)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not dacl_present.value or not dacl.value:
            raise OSError("generated Windows security descriptor has no DACL")
        result = advapi32.SetNamedSecurityInfoW(
            str(path),
            1,
            0x00000004 | 0x80000000,
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise OSError(result, ctypes.FormatError(result))
    finally:
        kernel32.LocalFree(descriptor)


def windows_private_dacl(path):
    """Return whether a Windows path has one protected current-user allow ACE.

    This is primarily an executable assertion for the Windows CI job.
    """
    if os.name != "nt":
        raise OSError("Windows DACL inspection is only available on Windows")
    ctypes, wintypes, advapi32, kernel32 = _windows_api()

    class ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", wintypes.WORD),
        ]

    class ACCESS_ALLOWED_ACE(ctypes.Structure):
        _fields_ = [
            ("Header", ACE_HEADER),
            ("Mask", wintypes.DWORD),
            ("SidStart", wintypes.DWORD),
        ]

    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL

    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,
        0x00000004,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise OSError(result, ctypes.FormatError(result))
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        info = ACL_SIZE_INFORMATION()
        if not advapi32.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.AceCount != 1 or not (control.value & 0x1000):
            return False
        ace_pointer = ctypes.c_void_p()
        if not advapi32.GetAce(dacl, 0, ctypes.byref(ace_pointer)):
            raise ctypes.WinError(ctypes.get_last_error())
        ace = ctypes.cast(ace_pointer, ctypes.POINTER(ACCESS_ALLOWED_ACE)).contents
        if ace.Header.AceType != 0 or ace.Mask != 0x001F01FF:
            return False
        sid_pointer = ctypes.c_void_p(ace_pointer.value + ACCESS_ALLOWED_ACE.SidStart.offset)
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return sid_text.value == _windows_current_user_sid()
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        kernel32.LocalFree(descriptor)


def restrict_private_path(path, logger=None):
    """Restrict an existing path; warn and continue if Windows ACL setup fails."""
    safe = validate_store_path(path)
    if os.name == "nt":
        try:
            _restrict_acl_windows(safe)
            return True
        except Exception as exc:  # Windows ACL failure must not corrupt the write
            _warn_once(
                f"could not restrict Windows ACL ({type(exc).__name__})",
                safe,
                logger,
            )
            return False
    os.chmod(safe, 0o700 if safe.is_dir() else 0o600)
    return True


def _existing_directory_warning(path, logger=None):
    if os.name == "nt":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & 0o077:
        _warn_once(
            f"existing store directory is accessible beyond the current user (mode {mode:04o}); permissions left unchanged",
            path,
            logger,
        )


def ensure_parent(path, *, private=True, logger=None, purpose="store"):
    """Create missing parents without changing permissions on existing ones."""
    safe = validate_store_path(path, purpose)
    parent = safe.parent
    missing = []
    cursor = parent
    while not cursor.exists():
        missing.append(cursor)
        next_cursor = cursor.parent
        if next_cursor == cursor:
            break
        cursor = next_cursor
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700 if private else 0o777)
            if private and _NOFOLLOW_DIRS:
                # chmod the directory just created, not whatever a racing
                # process swapped in at that name (scan finding 3).
                dir_fd = _open_dir_nofollow(directory)
                try:
                    os.fchmod(dir_fd, 0o700)
                finally:
                    os.close(dir_fd)
            elif private:
                restrict_private_path(directory, logger=logger)
        except FileExistsError:
            if private:
                _existing_directory_warning(directory, logger)
    if not missing and private:
        _existing_directory_warning(parent, logger)
    return safe


def ensure_private_dir(path, logger=None):
    """Compatibility helper: ensure the target file's private parent exists."""
    return ensure_parent(path, private=True, logger=logger)


class FileLock:
    """Portable advisory lock.

    On POSIX it is a kernel lock (``fcntl.flock``) on a kept lock file, taken
    without following symlinks: the kernel releases it when the holder exits or
    crashes, so nothing is ever broken as stale.

    Elsewhere (Windows) it uses atomic lock-file creation: a lock older than
    ``LOCK_STALE_SECONDS`` is treated as abandoned. This
    prevents permanent deadlock after a crash but can permit two writers after
    a process is suspended longer than that threshold. The residual lost-update
    risk is documented in SECURITY.md; these critical sections must stay short.
    """

    def __init__(self, target_path, *, stale_seconds=None, timeout_seconds=None, retry_interval=None):
        self.lock_path = str(target_path) + ".lock"
        self._fd = None
        self._dir_fd = None
        self._name = None
        self.stale_seconds = LOCK_STALE_SECONDS if stale_seconds is None else float(stale_seconds)
        self.timeout_seconds = LOCK_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds)
        self.retry_interval = LOCK_RETRY_INTERVAL if retry_interval is None else float(retry_interval)

    def __enter__(self):
        deadline = time.monotonic() + self.timeout_seconds
        safe = ensure_parent(Path(self.lock_path), private=True)
        if _NOFOLLOW_DIRS:
            return self._enter_nofollow(safe, deadline)
        while True:
            try:
                self._fd = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.write(self._fd, str(os.getpid()).encode("ascii"))
                if os.name == "nt":
                    restrict_private_path(Path(self.lock_path))
                return self
            except (FileExistsError, PermissionError):
                try:
                    age = time.time() - os.path.getmtime(self.lock_path)
                    if age > self.stale_seconds:
                        os.remove(self.lock_path)
                        continue
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"could not acquire lock {self.lock_path} within {self.timeout_seconds:g}s"
                        ) from None
                    time.sleep(self.retry_interval)
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire lock {self.lock_path} within {self.timeout_seconds:g}s"
                    ) from None
                time.sleep(self.retry_interval)

    def _enter_nofollow(self, safe, deadline):
        """POSIX: a kernel lock (flock) on a lock file opened relative to its
        no-follow directory. The kernel releases it when the holder exits or
        crashes, so there is no stale lock to break and no unlink race (scan
        finding 3); the lock file itself is kept, never removed."""
        self._dir_fd = _open_dir_nofollow(safe.parent)
        self._name = safe.name
        try:
            self._fd = _open_file_nofollow(self._dir_fd, self._name, os.O_CREAT | os.O_RDWR)
            if not stat.S_ISREG(os.fstat(self._fd).st_mode):
                raise StoreRedirectError(0, f"refusing non-file lock {self._name!r}")
            while True:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"could not acquire lock {self.lock_path} within {self.timeout_seconds:g}s"
                        ) from None
                    time.sleep(self.retry_interval)
        except BaseException:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            os.close(self._dir_fd)
            self._dir_fd = None
            raise

    def __exit__(self, exc_type, exc, tb):
        if self._dir_fd is not None:
            try:
                if self._fd is not None:
                    with contextlib.suppress(OSError):
                        fcntl.flock(self._fd, fcntl.LOCK_UN)
                    os.close(self._fd)
                    self._fd = None
            finally:
                os.close(self._dir_fd)
                self._dir_fd = None
            return False
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        with contextlib.suppress(OSError):
            os.remove(self.lock_path)
        return False


def unique_tmp_path(safe):
    safe = validate_store_path(safe)
    return safe.with_name(f".{safe.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp")


def atomic_replace(tmp, safe):
    """Replace with bounded retries for transient Windows sharing failures."""
    attempts = 5
    for attempt in range(attempts):
        try:
            os.replace(tmp, safe)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def _atomic_write_nofollow(safe, text, private):
    """POSIX atomic write relative to the parent directory (scan findings 3, 5):
    the temporary file, its mode, the rename and cleanup never follow a symlink,
    and a symlink at the target name is refused, not written through."""
    dir_fd = _open_dir_nofollow(safe.parent)
    tmp_name = unique_tmp_path(safe).name
    try:
        existing = _lstat_at(dir_fd, safe.name)
        if existing is not None and stat.S_ISLNK(existing):
            raise StoreRedirectError(0, f"refusing symlinked store file {safe.name!r}")
        fd = _open_file_nofollow(dir_fd, tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o666)
        try:
            if private:
                os.fchmod(fd, 0o600)
            elif existing is not None:
                os.fchmod(fd, stat.S_IMODE(existing))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                fd = None
                handle.write(str(text))
        finally:
            if fd is not None:
                os.close(fd)
        os.replace(tmp_name, safe.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name, dir_fd=dir_fd)
        os.close(dir_fd)
    return safe


def atomic_write_text(path, text, *, private=True, logger=None, purpose="output"):
    safe = ensure_parent(path, private=private, logger=logger, purpose=purpose)
    if _NOFOLLOW_DIRS:
        return _atomic_write_nofollow(safe, text, private)
    tmp = unique_tmp_path(safe)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    mode = 0o600 if private else 0o666
    existing_mode = None
    if not private and safe.exists() and os.name != "nt":
        with contextlib.suppress(OSError):
            existing_mode = stat.S_IMODE(safe.stat().st_mode)
    fd = os.open(tmp, flags, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = None
            handle.write(str(text))
        if private:
            restrict_private_path(tmp, logger=logger)
        elif existing_mode is not None:
            os.chmod(tmp, existing_mode)
        atomic_replace(tmp, safe)
        if private:
            restrict_private_path(safe, logger=logger)
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(OSError):
            os.remove(tmp)
    return safe


def atomic_write_json(path, value, *, private=True, logger=None, purpose="store", sort_keys=False):
    return atomic_write_text(
        path,
        json.dumps(value, indent=2, sort_keys=sort_keys) + "\n",
        private=private,
        logger=logger,
        purpose=purpose,
    )


def read_private_text(path):
    """Text of a private store file, or None when it does not exist.

    On POSIX the file is opened relative to its no-follow parent directory with
    O_NOFOLLOW, so a symlink raises StoreRedirectError instead of being read
    through (Codex Security scan, finding 5)."""
    safe = validate_store_path(path)
    if not _NOFOLLOW_DIRS:
        try:
            with open(safe, encoding="utf-8") as handle:
                return handle.read()
        except FileNotFoundError:
            return None
    try:
        dir_fd = _open_dir_nofollow(safe.parent)
    except FileNotFoundError:
        return None
    try:
        fd = _open_file_nofollow(dir_fd, safe.name, os.O_RDONLY)
    except FileNotFoundError:
        return None
    finally:
        os.close(dir_fd)
    with os.fdopen(fd, encoding="utf-8") as handle:
        return handle.read()


def _load_json(path, expected_type, empty_value, logger=None):
    try:
        safe = validate_store_path(path)
    except StorePathError as exc:
        if logger:
            logger(f"load_json refused: {exc}")
        return empty_value
    try:
        text = read_private_text(safe)
        if text is None:
            return empty_value
        value = json.loads(text)
        return value if isinstance(value, expected_type) else empty_value
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if logger:
            logger(f"load_json({safe}): {type(exc).__name__}: {exc}")
        return empty_value


def load_json_list(path, logger=None):
    return _load_json(path, list, [], logger)


def save_json_list(path, items, logger=None):
    return atomic_write_json(path, items, private=True, logger=logger)


def load_json_dict(path, logger=None):
    return _load_json(path, dict, {}, logger)


def save_json_dict(path, value, logger=None):
    return atomic_write_json(path, value, private=True, logger=logger)
