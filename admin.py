"""Local admin tool: upload a snapshot, rebuild, commit, push.

    make admin          # http://localhost:8502

Run on YOUR machine only, with YOUR git credentials. This is deliberately not
part of the deployed dashboard: pushing needs write access to the repo, and the
people who read the dashboard should not have it. Running it as a second local
page keeps the credential where it already lives.

It is a convenience wrapper, not a second code path -- it shells out to the
same `make build` and the same pre-commit hook the terminal runbook uses, so
nothing here can bypass the validation gate.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Callable

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
from src import config as C  # noqa: E402

ROOT = Path(__file__).parent
RAW_DIR = ROOT / "data" / "raw"
NAME_RE = C.SNAPSHOT_NAME_RE


def run_stream(cmd: list[str], on_line: Callable[[str], None]) -> int:
    """Run a command, feeding each line to `on_line` as it arrives.

    stderr is folded into stdout so a traceback and the log around it stay in
    one readable stream, matching what you would see running it by hand.
    """
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        on_line(line.rstrip("\n"))
    proc.wait()
    return proc.returncode


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def streamer(limit: int = 400) -> Callable[[str], None]:
    box, lines = st.empty(), []
    def on_line(l: str) -> None:
        lines.append(l)
        box.code("\n".join(lines[-limit:]))
    return on_line


st.set_page_config(page_title="Snapshot uploader", page_icon="🛠️",
                   layout="centered")
st.title("Snapshot uploader")
st.caption("Local only. Uploads land in your working copy, then the same "
           "`make build` and pre-commit hook the terminal runbook uses do the "
           "validating — nothing here bypasses that gate.")

if "run_id" not in st.session_state:
    st.session_state.run_id = 0
key = lambda n: f"{n}_{st.session_state.run_id}"  # noqa: E731  fresh widgets per run

with st.expander("Current git status"):
    st.code(git("status", "--short").stdout or "(clean)")

st.subheader("1 · Source workbook")
up = st.file_uploader(C.SNAPSHOT_EXAMPLE,
                      type="xlsx", key=key("src"))
dest_name = None
if up:
    if NAME_RE.match(up.name):
        dest_name = up.name
        st.success(f"Will stage as `data/raw/{dest_name}`")
    else:
        # Never silently rename to today: the date is a human label, but a
        # wrong one makes the reproducibility record lie about what arrived when.
        snap = st.date_input("Filename does not match the convention — pick the "
                             "snapshot date", value=date.today(), key=key("date"))
        final = st.checkbox("This is the FINAL record for its month",
                            key=key("final"))
        version = st.text_input("Report version", value="3.1", key=key("ver"))
        dest_name = (f"Report True Friend V{version} {snap:%d-%m-%Y}"
                     f"{'-Final' if final else ''}.xlsx")
        st.warning(f"Will stage as `data/raw/{dest_name}`")

st.subheader("2 · Run")
msg = st.text_input("Commit message", value=f"snapshot {date.today():%Y-%m-%d}",
                    key=key("msg"))
do_push = st.checkbox("Push after commit (triggers the redeploy)", value=True,
                      key=key("push"))

if st.button("▶ Run", type="primary", disabled=not dest_name, width="stretch"):
    # --- 1: save to disk (not staged in git yet) ---
    dest = RAW_DIR / dest_name
    with st.status("Saving…", expanded=True) as s:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(up.getvalue())
        st.write(f"saved `{dest.relative_to(ROOT)}`")
        s.update(label=f"Saved {dest_name}", state="complete")

    # --- 2: rebuild + validate, BEFORE git is touched at all ---
    with st.status("Rebuilding & validating…", expanded=True) as s:
        if run_stream([sys.executable, "-W", "ignore", "-m", "src.build"], streamer()) != 0:
            s.update(label="Build FAILED — nothing will be committed",
                     state="error")
            st.error("Fix the cause above, then run again. No git changes were "
                     "made.")
            st.stop()
        s.update(label="Build OK, validation passed", state="complete")

    # --- 3: commit (the pre-commit hook rebuilds again as the final gate) ---
    with st.status("Committing…", expanded=True) as s:
        add = git("add", str(dest.relative_to(ROOT)), "data/processed")
        if add.returncode != 0:
            s.update(label="git add failed", state="error")
            st.code(add.stderr)
            st.stop()
        diff = git("diff", "--cached", "--stat").stdout
        st.code(diff or "(nothing changed)")
        can_push = bool(diff.strip())
        if not can_push:
            s.update(label="Nothing changed — nothing to commit", state="complete")
        elif run_stream(["git", "commit", "-m", msg], streamer(200)) != 0:
            s.update(label="Commit blocked (pre-commit rebuild failed)",
                     state="error")
            st.stop()
        else:
            s.update(label="Committed", state="complete")

    # --- 4: push ---
    if do_push and can_push:
        with st.status("Pushing…", expanded=True) as s:
            if run_stream(["git", "push"], streamer(200)) != 0:
                s.update(label="Push FAILED — commit is local only", state="error")
                st.info("The commit is safe on your machine. Resolve the error, "
                        "then `git push`.")
                st.stop()
            s.update(label="Pushed — the deploy will pick this up shortly",
                     state="complete")
        st.success("Done.")
    elif can_push:
        st.info("Committed locally. Run `git push` when ready.")

    st.session_state.run_id += 1      # fresh upload widget on the next run
