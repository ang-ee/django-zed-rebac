"""Exercise the publish workflow's three-way version guard."""

import textwrap
from pathlib import Path

import pytest

import rebac


@pytest.mark.parametrize("different", [None, "tag", "project", "installed"])
def test_publish_version_agreement(monkeypatch, tmp_path, different):
    workflow = (Path(__file__).parents[1] / ".github/workflows/publish-pypi.yml").read_text()
    script = textwrap.dedent(
        workflow.split("python - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    )
    versions = dict.fromkeys(("tag", "project", "installed"), rebac.__version__)
    if different is not None:
        versions[different] = "0.0.0"
    monkeypatch.setenv("GITHUB_REF_NAME", f"v{versions['tag']}")
    monkeypatch.setenv("REBAC_INSTALLED_VERSION", versions["installed"])
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{versions["project"]}"\n')
    monkeypatch.chdir(tmp_path)
    if different is None:
        exec(compile(script, "publish-version-guard", "exec"), {})
    else:
        with pytest.raises(SystemExit) as error:
            exec(compile(script, "publish-version-guard", "exec"), {})
        assert error.value.code == 1
