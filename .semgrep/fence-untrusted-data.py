# ruff: noqa: F821 -- a semgrep fixture; the names are never defined.
# Rule test for fence-untrusted-data.yml. CI runs:
#   semgrep --test --config .semgrep/fence-untrusted-data.yml .semgrep/fence-untrusted-data.py
# `ruleid:` marks a line the rule must flag; `ok:` a line it must not.


def bare_unfenced(kql, since):
    out, err = bzrk_search(kql, since)
    # ruleid: unfenced-bzrk-output-reaches-return
    return out, err


def module_unfenced(kql, since):
    out, err = bm_runner.bzrk_search(kql, since)
    # ruleid: unfenced-bzrk-output-reaches-return
    return out, err


def module_run_bzrk_unfenced(argv):
    out, err = bm_runner.run_bzrk(argv)
    # ruleid: unfenced-bzrk-output-reaches-return
    return f"result: {out}", err


def bare_fenced(kql, since):
    out, err = bzrk_search(kql, since)
    # ok: unfenced-bzrk-output-reaches-return
    return _fence_untrusted(out), err


def module_fenced(kql, since):
    out, err = bm_runner.bzrk_search_json(kql, since)
    # ok: unfenced-bzrk-output-reaches-return
    return bm_fencing._fence_untrusted(out), err


def module_fence_limited(kql, since):
    out, err = bm_runner.bzrk_search(kql, since)
    # ok: unfenced-bzrk-output-reaches-return
    return bm_fencing._fence_limited(out), err


def module_envelope(kql, since):
    out, err = bm_runner.bzrk_search(kql, since)
    # ok: unfenced-bzrk-output-reaches-return
    return bm_tools._envelope("t", since, out, fence_body=True), err


def bare_json_unfenced(kql, since):
    out, err = bzrk_search_json(kql, since)
    # ruleid: unfenced-bzrk-output-reaches-return
    return out, err


def bare_run_bzrk_unfenced(argv):
    out, err = run_bzrk(argv)
    # ruleid: unfenced-bzrk-output-reaches-return
    return out, err


def module_json_unfenced(kql, since):
    out, err = bm_runner.bzrk_search_json(kql, since)
    # ruleid: unfenced-bzrk-output-reaches-return
    return out, err


def module_wrap_analytics_success_is_not_a_fence(kql, since):
    out, err = bm_runner.bzrk_search_json(kql, since)
    # _wrap_analytics fences only error text; success text passes through.
    text, is_err = bm_fencing._wrap_analytics((out, err))
    # ruleid: unfenced-bzrk-output-reaches-return
    return text, is_err


def bare_envelope(kql, since):
    out, err = bzrk_search(kql, since)
    # ok: unfenced-bzrk-output-reaches-return
    return _envelope("t", since, out, fence_body=True), err


def bare_wrap_analytics_success_is_not_a_fence(kql, since):
    out, err = bzrk_search_json(kql, since)
    text, is_err = _wrap_analytics((out, False))
    # ruleid: unfenced-bzrk-output-reaches-return
    return text, is_err


def bare_fence_limited(kql, since):
    out, err = bzrk_search(kql, since)
    # ok: unfenced-bzrk-output-reaches-return
    return _fence_limited(out), err


def lookalike_receiver_fence_untrusted(kql, since):
    out, err = bm_runner.bzrk_search(kql, since)
    # Only the real fencing module sanitizes; any other receiver does not.
    # ruleid: unfenced-bzrk-output-reaches-return
    return passthrough._fence_untrusted(out), err


def lookalike_receiver_fence_limited(kql, since):
    out, err = bm_runner.bzrk_search(kql, since)
    # ruleid: unfenced-bzrk-output-reaches-return
    return passthrough._fence_limited(out), err


def lookalike_receiver_envelope(kql, since):
    out, err = bm_runner.bzrk_search(kql, since)
    # ruleid: unfenced-bzrk-output-reaches-return
    return passthrough._envelope("t", since, out, fence_body=True), err
