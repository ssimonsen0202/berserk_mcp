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


LOCK_STALE_SECONDS = 30
LOCK_TIMEOUT_SECONDS = 10
LOCK_RETRY_INTERVAL = 0.05

_WARNED_PATHS = set()
_WARN_LOCK = threading.Lock()


class StorePathError(ValueError):
    """Raised when a caller-supplied filesystem path is unsafe."""


def validate_store_path(candidate, purpose="store"):
    """Return a resolved absolute path after rejecting traversal and controls."""
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
    resolved = candidate_path.resolve(strict=False)
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


def _windows_api():
    """Return configured Windows security DLL handles and ctypes types."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
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
        if not advapi32.GetTokenInformation(
            token, 1, buffer, needed.value, ctypes.byref(needed)
        ):
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
            descriptor, ctypes.byref(dacl_present), ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not dacl_present.value or not dacl.value:
            raise OSError("generated Windows security descriptor has no DACL")
        result = advapi32.SetNamedSecurityInfoW(
            str(path), 1, 0x00000004 | 0x80000000,
            None, None, dacl, None,
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
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL

    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path), 1, 0x00000004,
        None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
    )
    if result:
        raise OSError(result, ctypes.FormatError(result))
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        info = ACL_SIZE_INFORMATION()
        if not advapi32.GetAclInformation(
            dacl, ctypes.byref(info), ctypes.sizeof(info), 2
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.AceCount != 1 or not (control.value & 0x1000):
            return False
        ace_pointer = ctypes.c_void_p()
        if not advapi32.GetAce(dacl, 0, ctypes.byref(ace_pointer)):
            raise ctypes.WinError(ctypes.get_last_error())
        ace = ctypes.cast(ace_pointer, ctypes.POINTER(ACCESS_ALLOWED_ACE)).contents
        if ace.Header.AceType != 0 or ace.Mask != 0x001F01FF:
            return False
        sid_pointer = ctypes.c_void_p(
            ace_pointer.value + ACCESS_ALLOWED_ACE.SidStart.offset
        )
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
                f"could not restrict Windows ACL ({type(exc).__name__})", safe, logger,
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
            if private:
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
    """Portable advisory lock using atomic lock-file creation.

    A lock older than ``LOCK_STALE_SECONDS`` is treated as abandoned. This
    prevents permanent deadlock after a crash but can permit two writers after
    a process is suspended longer than that threshold. The residual lost-update
    risk is documented in SECURITY.md; these critical sections must stay short.
    """

    def __init__(self, target_path, *, stale_seconds=None, timeout_seconds=None,
                 retry_interval=None):
        self.lock_path = str(target_path) + ".lock"
        self._fd = None
        self.stale_seconds = (
            LOCK_STALE_SECONDS if stale_seconds is None else float(stale_seconds)
        )
        self.timeout_seconds = (
            LOCK_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds)
        )
        self.retry_interval = (
            LOCK_RETRY_INTERVAL if retry_interval is None else float(retry_interval)
        )

    def __enter__(self):
        deadline = time.monotonic() + self.timeout_seconds
        ensure_parent(Path(self.lock_path), private=True)
        while True:
            try:
                self._fd = os.open(
                    self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600,
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
                            f"could not acquire lock {self.lock_path} within "
                            f"{self.timeout_seconds:g}s"
                        ) from None
                    time.sleep(self.retry_interval)
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire lock {self.lock_path} within "
                        f"{self.timeout_seconds:g}s"
                    )
                time.sleep(self.retry_interval)

    def __exit__(self, exc_type, exc, tb):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            os.remove(self.lock_path)
        except OSError:
            pass
        return False


def unique_tmp_path(safe):
    safe = validate_store_path(safe)
    return safe.with_name(
        f".{safe.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    )


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


def atomic_write_text(path, text, *, private=True, logger=None, purpose="output"):
    safe = ensure_parent(path, private=private, logger=logger, purpose=purpose)
    tmp = unique_tmp_path(safe)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    mode = 0o600 if private else 0o666
    existing_mode = None
    if not private and safe.exists() and os.name != "nt":
        try:
            existing_mode = stat.S_IMODE(safe.stat().st_mode)
        except OSError:
            pass
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
        try:
            os.remove(tmp)
        except OSError:
            pass
    return safe


def atomic_write_json(path, value, *, private=True, logger=None, purpose="store",
                      sort_keys=False):
    return atomic_write_text(
        path,
        json.dumps(value, indent=2, sort_keys=sort_keys) + "\n",
        private=private,
        logger=logger,
        purpose=purpose,
    )


def _load_json(path, expected_type, empty_value, logger=None):
    try:
        safe = validate_store_path(path)
    except StorePathError as exc:
        if logger:
            logger(f"load_json refused: {exc}")
        return empty_value
    try:
        with open(safe, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, expected_type) else empty_value
    except FileNotFoundError:
        return empty_value
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
