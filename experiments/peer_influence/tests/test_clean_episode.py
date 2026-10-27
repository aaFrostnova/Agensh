import os
import subprocess
import time

from peer_influence.scripts.clean_episode import clean_episode, processes_using


def test_only_processes_that_reference_the_live_root_are_found_and_stopped(tmp_path):
    live = tmp_path / "live" / "ep_000001"
    live.mkdir(parents=True)
    other = tmp_path / "live" / "ep_000002"
    other.mkdir()
    mine = subprocess.Popen(["sh", "-c", "sleep 37", str(live / "marker")], start_new_session=True)
    theirs = subprocess.Popen(["sh", "-c", "sleep 38", str(other / "marker")], start_new_session=True)
    try:
        time.sleep(0.3)
        assert processes_using(live) == [mine.pid]
        report = clean_episode(live)
        assert report["killed"] == [mine.pid] and not live.exists()
        assert mine.wait(timeout=5) is not None
        assert theirs.poll() is None and other.exists()  # a neighbouring episode is untouched
    finally:
        theirs.kill()
        theirs.wait()


def test_the_calling_process_is_never_killed_even_when_its_cwd_is_inside(tmp_path):
    live = tmp_path / "ep"
    live.mkdir()
    cwd = os.getcwd()
    os.chdir(live)
    try:
        assert os.getpid() not in processes_using(live)
        clean_episode(live)
    finally:
        os.chdir(cwd)
