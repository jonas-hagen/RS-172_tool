# Releasing to PyPI

Releases are built and uploaded by GitHub Actions (`.github/workflows/release.yml`) when a `v*` tag is pushed. The upload uses PyPI trusted publishing, so no API token is stored anywhere.

## One-time setup

1. **PyPI:** sign in at pypi.org and open *Your account → Publishing → Add a new pending publisher*. Enter:
   - PyPI project name: `rs172`
   - Owner: the GitHub user or organisation that owns the repository
   - Repository name: `RS-172_tool`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
2. **GitHub:** in the repository, open *Settings → Environments* and create an environment named `pypi`. Optionally require your approval before each deployment.
3. **GitHub:** make `main` the default branch. The README links point to `main`, so PyPI shows the photo and links only once `main` exists.

## Each release

1. Set the new version in `src/rs172/__init__.py` (`__version__`). It is the only place the version is defined.
2. Commit, push to `main`, and wait for CI to pass.
3. Tag and push:
   ```
   git tag v0.1.0
   git push origin v0.1.0
   ```
   The workflow runs the tests, checks that the tag matches `__version__`, builds the sdist and wheel, checks them with twine, and uploads them to PyPI.

## Check locally before tagging (optional)

```
pip install -e ".[dev]"
python -m build
python -m twine check --strict dist/*
```

For a dry run on TestPyPI, upload `dist/*` there with `twine upload -r testpypi dist/*`. This needs a TestPyPI account and token.
