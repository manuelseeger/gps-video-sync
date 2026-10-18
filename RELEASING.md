# Releases

## Tag formats

| Git tag | Package version | GitHub release | Package index | Approval environment |
| --- | --- | --- | --- | --- |
| `3.2.1` | `3.2.1` | Release | PyPI | `pypi` |
| `3.2.1-preview.1` | `3.2.1rc1` | Pre-release | TestPyPI | `testpypi` |

Use numeric `major.minor.patch` tags without a `v` prefix. Preview numbers start
at 1. Python normalizes `preview` to `rc`; use the canonical `rcN` version in
`pyproject.toml` and `src/gps_video_sync/__init__.py`.

## Trusted Publishing and approval

Configure a GitHub Trusted Publisher on each index. Use these values:

- Owner: `manuelseeger`
- Repository: `gps-video-sync`
- Workflow filename: `publish.yml`
- Project: `gps-video-sync`
- Environment: `pypi` on PyPI, `testpypi` on TestPyPI

For a new project, register a pending publisher in the index account's Publishing
settings. Both index accounts need two-factor authentication.

In GitHub repository settings, configure the `pypi` and `testpypi` environments
with required reviewers. Allow version tags for `pypi` and preview tags for
`testpypi`. If deployment tag rules are restricted, make sure they permit the
full tag format, including `-preview.N`. Single-maintainer repositories can allow
self-approval. Do not bypass environment approval to publish a release.

The upload jobs use GitHub OIDC and short-lived credentials. No PyPI upload token
or repository secret is required. A GitHub Release uses the run's `GITHUB_TOKEN`.

## Publish

1. Set the same canonical package version in `pyproject.toml` and
   `src/gps_video_sync/__init__.py`.
2. Review release archive contents for private data and unintended files. Run
   tests, commit the changes, and push them to `main`.
3. Tag that commit and push the tag:

   ```sh
   # TestPyPI preview (package version 3.2.1rc1)
   git tag 3.2.1-preview.1
   git push origin 3.2.1-preview.1

   # Production release, after a separate version commit (package version 3.2.1)
   git tag 3.2.1
   git push origin 3.2.1
   ```

4. Open Actions → Release and publish. The workflow validates the tag and requires
   its commit to be on `main`. It runs CI, builds and checks both archives, and
   checks that the API and package versions match the tag's canonical version.
5. The workflow creates the GitHub Release or pre-release, with the tested wheel
   and source archive attached. The release exists before upload approval.
6. Review the release and artifacts, then approve the pending `pypi` or `testpypi`
   deployment. Approval uploads only to the index selected by the tag.
7. The workflow compares both index archive hashes with the tested build,
   downloads the exact wheel, and tests the installed package and `verify` extra
   outside the checkout. Confirm these checks pass and inspect the index page.

Stable tags publish directly to PyPI; they do not upload to TestPyPI. Test a
preview before the first production release. Pushing a tag starts the workflow;
creating a GitHub Release alone does not start it.

## Retry and corrections

Use Actions → Re-run failed jobs for transient failures. Manual dispatch is also
available when a tag is selected. Branch dispatches and unsupported tags fail
validation.

An existing GitHub Release is accepted only when its attached archives match the
current build. Existing index files are skipped during upload and then checked
against the build's hashes. A mismatch fails verification. Never replace release
assets, move a published tag, or overwrite published package files. Package
indexes do not permit file replacement. Corrections require a new version and
tag; a new preview needs a higher preview number.
