import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.integration
@pytest.mark.parametrize("script", ["smoke_installed.py", "check_independent_videos.py"])
def test_verification_script_accepts_symlinked_tmpdir(tmp_path, script):
    pytest.importorskip("matplotlib")
    if script == "check_independent_videos.py" and shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required")
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    env = dict(os.environ, TMPDIR=str(alias))
    if script == "smoke_installed.py":
        pytest.importorskip("build")
        pytest.importorskip("hatchling")
        wheels = tmp_path / "wheels"
        built = subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation",
                                "--outdir", str(wheels)], cwd=ROOT, env=env,
                               capture_output=True, text=True)
        assert built.returncode == 0, built.stdout + built.stderr
        installed = tmp_path / "site-packages"
        with zipfile.ZipFile(next(wheels.glob("*.whl"))) as wheel:
            wheel.extractall(installed)
        binaries = tmp_path / "bin"
        binaries.mkdir()
        command = binaries / "gps-video-sync"
        command.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m gps_video_sync "$@"\n')
        command.chmod(0o755)
        env.update(PYTHONPATH=str(installed), PATH=str(binaries) + os.pathsep + env["PATH"])
    process = subprocess.run([sys.executable, str(ROOT / "scripts" / script)],
                             cwd=tmp_path, env=env, capture_output=True, text=True)
    assert process.returncode == 0, process.stdout + process.stderr
    expected = ("Installed API and CLI smoke tests passed" if script == "smoke_installed.py"
                else "Independent videos verified")
    assert expected in process.stdout
