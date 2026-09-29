"""platform_check's own bookkeeping: a check that outlasts its timeout is reported, not a crash."""

from pathlib import Path

from beamer2slides.devtools import platform_check


def test_a_check_past_its_timeout_is_exit_minus_one(tmp_path: Path) -> None:
    """On Windows `subprocess.run(text=True)` hands a timed-out process's output over as str,
    which the run once called `.decode` on and died with an AttributeError."""
    r = platform_check.run("slow", "platform_check", ["--help"], tmp_path, 0.001)
    assert r.exit == -1 and r.check == "slow"
    assert (tmp_path / "slow" / "log.txt").read_text(encoding="utf-8").startswith("timeout after 0.001 s\n")


def test_printed_takes_bytes_str_and_nothing() -> None:
    assert platform_check.printed(b"caf\xc3\xa9") == "café"
    assert platform_check.printed("done") == "done"
    assert platform_check.printed(None) == ""
