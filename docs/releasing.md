# Releases

## Package contract

- Distribution and command: `gps-video-sync`.
- Python package: `gps_video_sync`.
- Supported Python versions: 3.10–3.15; CI tests each version.
- Version: `0.1.0`, in `pyproject.toml` and `src/gps_video_sync/__init__.py`.
- Git tags use `major.minor.patch`, for example `0.1.0`, with no `v` prefix.
- Runtime dependencies: NumPy and SciPy. The `verify` extra adds Matplotlib.
- GoPro extraction needs no external programs. Frame extraction needs the
  system `ffmpeg` executable. Python package installation does not install it.
- License: MIT. Each archive includes `LICENSE`.
- Build backend: Hatchling 1.32.4. GitHub Actions are pinned to commit hashes.

Published files cannot be replaced. A package fix requires a new version and
matching Git tag. Use the same version for TestPyPI and PyPI validation.

## Publisher setup

1. Check the current package name at <https://pypi.org/project/gps-video-sync/>
   and <https://test.pypi.org/project/gps-video-sync/>. A missing project page
   does not reserve the name or guarantee that registration is allowed.
2. Create separate PyPI and TestPyPI accounts. Enable two-factor authentication
   on both accounts and store recovery codes securely.
3. In GitHub repository settings, create environments `testpypi` and `pypi`.
   Set required reviewers for release approval. Restrict deployments to
   tags with the pattern `[0-9]*.[0-9]*.[0-9]*` (tag rules only, no branch rules).
   The workflow also checks the exact version format and requires the tagged
   commit to be on `main`. Review protection rules before the first workflow run;
   a workflow's `environment` field alone does not enforce approval.
4. In each index, add a Trusted Publisher (or a pending publisher for a new
   project) with these exact values:

   | Field | Value |
   | --- | --- |
   | PyPI project | `gps-video-sync` |
   | GitHub owner | `manuelseeger` |
   | Repository | `gps-video-sync` |
   | Workflow filename | `publish.yml` |
   | Environment | `testpypi` on TestPyPI; `pypi` on PyPI |

   Use <https://pypi.org/manage/account/publishing/> and
   <https://test.pypi.org/manage/account/publishing/> for pending publishers.
   See [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/).
5. Do not add upload tokens to repository files or GitHub secrets. The publish
   jobs request short-lived OIDC credentials with `id-token: write`.

Account access, two-factor authentication, publisher registration, environment
reviewers, and release approval require a maintainer.

## Local validation

Install `uv`, FFmpeg, and the supported Python versions. From a checkout:

```sh
uv run --python 3.10 --extra dev --extra verify pytest -q
uv run --python 3.15 --extra dev --extra verify pytest -q
```

The full suite needs the tracked `tests/fixtures` files, `ffmpeg`, and Matplotlib.
The optional parity tests need `ffprobe` 9 or later and skip with older versions.
Do not treat other skipped integration tests as release validation.
The real-flight fixtures contain coordinates, flight times, and media. They
are for repository tests and are excluded from release archives.

Start with an empty `dist/` directory (move previous builds elsewhere):

```sh
uv build
python scripts/check_release.py dist --version 0.1.0
uvx twine check --strict dist/*
```

The archive checker uses an exact file allowlist. Only package code, metadata,
README, license, and documentation are allowed. Review all archive text for
private paths, credentials, GPS coordinates, flight times, and identifying
information. The allowlist is not a content privacy scan. Do not publish
tracks, recordings, telemetry caches, `sync.json`, plots, or other local output.
Keep private recordings and logs out of Git.

Test a clean installation outside the checkout (POSIX shell):

```sh
repo="$PWD"
wheel=$(find "$repo/dist" -maxdepth 1 -name '*.whl')
smoke=$(mktemp -d)
uv venv --python 3.15 "$smoke/venv"
uv pip install --python "$smoke/venv/bin/python" "$wheel"
export PATH="$smoke/venv/bin:$PATH"
cd "$smoke"
python -I "$repo/scripts/smoke_installed.py"
uv pip install --python "$smoke/venv/bin/python" "$wheel[verify]"
python -I "$repo/scripts/smoke_installed.py" --verify
```

The smoke test checks package/API versions, imports, module and command help,
synthetic GPS synchronization through the API and CLI, and optional plotting.
The CI `build` job uses Python 3.15 with the uv cache disabled. It builds the
archives, runs the standard-library-only archive checker, and uploads the
immutable `distributions` artifact before any dependency smoke tests run.
A separate `metadata` job downloads that artifact and runs `twine check --strict`.
The `smoke` jobs download the same artifact, build a wheel from its source
archive, and test installed wheels on Python 3.10 and 3.15. These tests include
the `verify` extra, real GoPro extraction, and motion matching with the fixtures.
The test and smoke jobs install FFmpeg for frame extraction. The distribution FFmpeg on the CI runner is older than
version 9, so CI skips the optional parity tests. Package execution
does not need `ffprobe`. The Python 3.10 test job checks the lowest direct
dependency versions.

## Publish

1. Update both package versions. Review metadata, documentation URLs, dependency
   ranges, supported Python versions, archive contents, and test results.
2. Commit the release changes and push to `main`. Wait for CI to pass.
3. Create and push the matching tag on that tested commit, for example:

   ```sh
   git tag 0.1.0
   git push origin 0.1.0
   ```

4. Run the **Publish** workflow on that tag. Leave `production` disabled for
   a TestPyPI-only trial. Approve the `testpypi` deployment. The workflow runs
   the complete CI suite and publishes the approved artifact with Trusted
   Publishing. The installation job first downloads the `distributions`
   artifact, then downloads the exact TestPyPI wheel without dependencies.
   Before installation or package execution, it compares the wheel bytes with
   the approved wheel and checks both index archive SHA-256 hashes with
   `scripts/check_index.py --index testpypi`. Only after these checks pass does
   it install the wheel outside the checkout and test the `verify` extra.
   Dependencies are installed from PyPI, not from TestPyPI.
5. Inspect the TestPyPI project page: README, project links, license, supported
   Python versions, and dependencies. Review the workflow's distribution
   artifact before production approval.
6. Run **Publish** on the same tag with `production` enabled. TestPyPI upload
   permits existing files; the downloaded wheel must still match the build.
   Approve the `pypi` environment only after all checks and text review pass.
   The production job uploads the same artifact, without rebuilding it.
7. Confirm the production installation job passes. It downloads the approved
   `distributions` artifact and runs
   `python scripts/check_index.py dist --version "$VERSION" --index pypi`.
   This standard-library-only check compares both archive hashes and retries
   while the index updates. A failed comparison blocks installation and package
   execution. After the check passes, the job installs both the base package
   and `gps-video-sync[verify]` from PyPI and runs installed smoke tests.
   Check the rendered PyPI project page and all documentation links.
8. Create a GitHub release for the tag and record the release test results.

If publication succeeds but a later check fails, do not move the tag or replace
files. Diagnose the failure and use a new package version for any package fix.
If an existing TestPyPI release differs from the current artifact, use a new
version for both indexes. Do not bypass the comparison check.
