# ruff: noqa: F821 -- a semgrep fixture; the names are never defined.
# Rule test for fence-untrusted-data.yml. Run: semgrep --test .semgrep/
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
