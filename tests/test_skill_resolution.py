import subprocess

from master import MasterOrchestrator


def make_master(tmp_path):
    return MasterOrchestrator(root=tmp_path, agents=[])


def test_extract_skill_reference_from_user_wording(tmp_path):
    master = make_master(tmp_path)

    refs = master._extract_skill_refs(
        "A skill dele será code-reviewer (existente no Playbooks/claude-vibe), atuando como Security Scanner."
    )

    assert refs == ["code-reviewer"]


def test_extract_skill_reference_handles_wrapped_hyphenated_name(tmp_path):
    master = make_master(tmp_path)

    refs = master._extract_skill_refs(
        "A skill dele será codebase-a\nnalyzer (existente no Playbooks/claude-vibe)."
    )

    assert refs == ["codebase-analyzer"]


def test_extract_skill_reference_from_url_does_not_add_partial_tokens(tmp_path):
    master = make_master(tmp_path)

    refs = master._extract_skill_refs(
        "A skill dele será https://github.com/AK3847/Codebase-Analyzer, atuando como analista."
    )

    assert refs == ["https://github.com/AK3847/Codebase-Analyzer"]


def test_augment_skill_prompt_loads_markdown_file(tmp_path):
    skill_dir = tmp_path / "Playbooks" / "claude-vibe"
    skill_dir.mkdir(parents=True)
    (skill_dir / "code-reviewer.md").write_text("Use checklist OWASP e reporte severidade.", encoding="utf-8")
    master = make_master(tmp_path)

    prompt, loaded = master._augment_skill_prompt("A skill dele será code-reviewer.")

    assert loaded == ["Playbooks/claude-vibe/code-reviewer.md"]
    assert "SKILL CARREGADA DO PROJETO" in prompt
    assert "Use checklist OWASP" in prompt


def test_augment_skill_prompt_loads_skill_directory(tmp_path):
    skill_dir = tmp_path / "Playbooks" / "claude-vibe" / "code-reviewer"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Analise complexidade e code smells.", encoding="utf-8")
    master = make_master(tmp_path)

    prompt, loaded = master._augment_skill_prompt("skill: code-reviewer")

    assert loaded == ["Playbooks/claude-vibe/code-reviewer"]
    assert "Analise complexidade" in prompt


def test_agent_name_can_resolve_matching_skill_with_hyphen_variant(tmp_path):
    skill_dir = tmp_path / "Playbooks" / "claude-vibe" / "codebase-analyzer"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Mapeie módulos, dependências e riscos da base.", encoding="utf-8")
    master = make_master(tmp_path)

    prompt, loaded = master._augment_skill_prompt(
        "Voce analisa a base de codigo.",
        agent_name="codebase_analyzer",
    )

    assert loaded == ["Playbooks/claude-vibe/codebase-analyzer"]
    assert "Mapeie módulos" in prompt


def test_extract_skill_reference_expands_underscore_hyphen_variants(tmp_path):
    master = make_master(tmp_path)

    refs = master._skill_ref_variants("codebase_analyzer")

    assert refs == ["codebase_analyzer", "codebase-analyzer"]


def test_augment_skill_prompt_downloads_known_catalog_skill(tmp_path):
    remote = tmp_path / "remote-code-reviewer.md"
    remote.write_text("Checklist remoto de code review.", encoding="utf-8")
    playbooks = tmp_path / "Playbooks"
    playbooks.mkdir()
    (playbooks / "skill-catalog.json").write_text(
        """
        {
          "skills": {
            "claude-vibe/code-reviewer": {
              "url": "%s"
            }
          },
          "aliases": {
            "code-reviewer": "claude-vibe/code-reviewer"
          }
        }
        """
        % remote.as_uri(),
        encoding="utf-8",
    )
    master = make_master(tmp_path)

    prompt, loaded = master._augment_skill_prompt("A skill dele será code-reviewer.")

    assert loaded == ["Playbooks/.cache/code-reviewer"]
    assert "Checklist remoto de code review." in prompt
    assert (tmp_path / "Playbooks" / ".cache" / "code-reviewer" / "SKILL.md").is_file()


def test_catalog_download_can_be_disabled(tmp_path, monkeypatch):
    remote = tmp_path / "remote-code-reviewer.md"
    remote.write_text("Checklist remoto de code review.", encoding="utf-8")
    playbooks = tmp_path / "Playbooks"
    playbooks.mkdir()
    (playbooks / "skill-catalog.json").write_text(
        '{"skills": {"code-reviewer": {"url": "%s"}}}' % remote.as_uri(),
        encoding="utf-8",
    )
    monkeypatch.setenv("MATTED_SKILL_AUTO_DOWNLOAD", "0")
    master = make_master(tmp_path)

    prompt, loaded = master._augment_skill_prompt("skill: code-reviewer")

    assert loaded == []
    assert "Checklist remoto" not in prompt


def test_web_search_extracts_duckduckgo_redirect_url(tmp_path):
    master = make_master(tmp_path)
    page = (
        '<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Forg%2Frepo%2Fblob%2Fmain%2FSKILL.md">'
        "result</a>"
    )

    urls = master._extract_search_result_urls(page)

    assert urls == ["https://github.com/org/repo/blob/main/SKILL.md"]


def test_web_candidate_normalizes_github_blob_to_raw(tmp_path):
    master = make_master(tmp_path)

    url = master._normalize_skill_candidate_url("https://github.com/org/repo/blob/main/skills/code-reviewer/SKILL.md")

    assert url == "https://raw.githubusercontent.com/org/repo/main/skills/code-reviewer/SKILL.md"


def test_web_search_saves_candidates_without_auto_download(tmp_path, monkeypatch):
    remote = tmp_path / "remote-skill.md"
    remote.write_text("Skill web encontrada.", encoding="utf-8")
    monkeypatch.setenv("MATTED_ALLOW_FILE_SKILL_URLS", "1")
    monkeypatch.setenv("MATTED_SKILL_WEB_AUTO_DOWNLOAD", "0")
    master = make_master(tmp_path)
    master._fetch_url_text = lambda url: '<a href="%s">skill</a>' % remote.as_uri()

    prompt, loaded = master._augment_skill_prompt("skill: web-reviewer")

    assert loaded == []
    assert "Skill web encontrada." not in prompt
    assert (tmp_path / "Playbooks" / ".cache" / ".web-candidates" / "web-reviewer.json").is_file()


def test_web_search_can_auto_download_when_enabled(tmp_path, monkeypatch):
    remote = tmp_path / "remote-skill.md"
    remote.write_text("Skill web aprovada.", encoding="utf-8")
    monkeypatch.setenv("MATTED_ALLOW_FILE_SKILL_URLS", "1")
    monkeypatch.setenv("MATTED_SKILL_WEB_AUTO_DOWNLOAD", "1")
    master = make_master(tmp_path)
    master._fetch_url_text = lambda url: '<a href="%s">skill</a>' % remote.as_uri()

    prompt, loaded = master._augment_skill_prompt("skill: web-reviewer")

    assert loaded == ["Playbooks/.cache/web-reviewer"]
    assert "Skill web aprovada." in prompt


def test_augment_skill_prompt_downloads_direct_skill_url(tmp_path):
    remote = tmp_path / "remote-direct-skill.md"
    remote.write_text("Skill direta por URL.", encoding="utf-8")
    master = make_master(tmp_path)

    prompt, loaded = master._augment_skill_prompt(f"A skill dele será {remote.as_uri()}.")

    assert loaded == ["Playbooks/.cache/remote-direct-skill"]
    assert "Skill direta por URL." in prompt


def test_augment_skill_prompt_reads_preserved_skill_reference_url(tmp_path):
    remote = tmp_path / "preserved-skill.md"
    remote.write_text("Skill preservada por URL.", encoding="utf-8")
    master = make_master(tmp_path)

    prompt, loaded = master._augment_skill_prompt(f"Voce analisa projetos.\n\nSkill reference: {remote.as_uri()}")

    assert loaded == ["Playbooks/.cache/preserved-skill"]
    assert "Skill preservada por URL." in prompt


def test_augment_skill_prompt_does_not_lookup_agent_name_when_explicit_skill_exists(tmp_path, monkeypatch):
    remote = tmp_path / "explicit-skill.md"
    remote.write_text("Skill explicita.", encoding="utf-8")
    master = make_master(tmp_path)
    looked_up = []

    original_download_catalog_skill = master._download_catalog_skill

    def track_catalog_lookup(skill_ref):
        looked_up.append(skill_ref)
        return original_download_catalog_skill(skill_ref)

    monkeypatch.setattr(master, "_download_catalog_skill", track_catalog_lookup)

    prompt, loaded = master._augment_skill_prompt(
        f"A skill dele será {remote.as_uri()}",
        agent_name="analise_projetos",
    )

    assert loaded == ["Playbooks/.cache/explicit-skill"]
    assert "Skill explicita." in prompt
    assert "analise_projetos" not in looked_up


def test_augment_skill_prompt_downloads_from_github_repo_url_common_paths(tmp_path, monkeypatch):
    master = make_master(tmp_path)
    fetched = {}

    def fake_download(url, destination, sha256=None):
        fetched["url"] = url
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("Skill via repo URL.", encoding="utf-8")

    monkeypatch.setattr(master, "_download_skill_file", fake_download)

    prompt, loaded = master._augment_skill_prompt("A skill dele será https://github.com/acme/demo-skill.")

    assert loaded == ["Playbooks/.cache/demo-skill"]
    assert "Skill via repo URL." in prompt
    assert fetched["url"].startswith("https://raw.githubusercontent.com/acme/demo-skill/")


def test_direct_repo_readme_is_wrapped_as_derived_skill(tmp_path, monkeypatch):
    master = make_master(tmp_path)

    def fake_download(url, destination, sha256=None):
        if "readme.md" not in url.lower():
            raise OSError("not found")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("# Codebase Analyzer\n\nHow to use this repository.", encoding="utf-8")

    monkeypatch.setattr(master, "_download_skill_file", fake_download)

    prompt, loaded = master._augment_skill_prompt("A skill dele será https://github.com/acme/readme-only-repo.")

    assert loaded == ["Playbooks/.cache/readme-only-repo"]
    assert "SKILL DERIVED FROM REPOSITORY README" in prompt
    assert "Source: https://github.com/acme/readme-only-repo" in prompt
    assert "How to use this repository." in prompt


def test_github_tree_url_loads_skill_from_subpath(tmp_path, monkeypatch):
    master = make_master(tmp_path)
    cache_dir = tmp_path / "Playbooks" / ".cache" / "code-reviewer"
    skill_dir = cache_dir / "code-reviewer"
    (cache_dir / ".git").mkdir(parents=True)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Use only the code-reviewer skill.", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        class Result:
            stdout = "origin\n"
            stderr = ""

        return Result()

    monkeypatch.setattr("master.subprocess.run", fake_run)

    prompt, loaded = master._augment_skill_prompt(
        "A skill dele será https://github.com/mamamou/ai-coding-skills/tree/main/code-reviewer."
    )

    assert loaded == ["Playbooks/.cache/code-reviewer/code-reviewer"]
    assert "Use only the code-reviewer skill." in prompt


def test_github_tree_url_raw_fallback_uses_branch_and_subpath(tmp_path, monkeypatch):
    master = make_master(tmp_path)
    fetched = []

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="git unavailable")

    def fake_download(url, destination, sha256=None):
        fetched.append(url)
        if url.endswith("/main/code-reviewer/SKILL.md"):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("Skill from raw subpath.", encoding="utf-8")
            return
        raise OSError("not found")

    monkeypatch.setattr("master.subprocess.run", fake_run)
    monkeypatch.setattr(master, "_download_skill_file", fake_download)

    prompt, loaded = master._augment_skill_prompt(
        "skill: https://github.com/mamamou/ai-coding-skills/tree/main/code-reviewer"
    )

    assert loaded == ["Playbooks/.cache/code-reviewer"]
    assert "Skill from raw subpath." in prompt
    assert fetched[0] == "https://raw.githubusercontent.com/mamamou/ai-coding-skills/main/code-reviewer/SKILL.md"


def test_github_tree_url_does_not_load_repo_root_when_subpath_missing(tmp_path, monkeypatch):
    master = make_master(tmp_path)
    cache_dir = tmp_path / "Playbooks" / ".cache" / "code-reviewer"
    (cache_dir / ".git").mkdir(parents=True)
    (cache_dir / "SKILL.md").write_text("Wrong root skill.", encoding="utf-8")
    fetched = []

    def fake_run(cmd, **kwargs):
        class Result:
            stdout = "origin\n"
            stderr = ""

        return Result()

    def fake_download(url, destination, sha256=None):
        fetched.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("Correct raw subpath skill.", encoding="utf-8")

    monkeypatch.setattr("master.subprocess.run", fake_run)
    monkeypatch.setattr(master, "_download_skill_file", fake_download)

    prompt, loaded = master._augment_skill_prompt(
        "skill: https://github.com/mamamou/ai-coding-skills/tree/main/code-reviewer"
    )

    assert loaded == ["Playbooks/.cache/code-reviewer"]
    assert "Correct raw subpath skill." in prompt
    assert "Wrong root skill." not in prompt
    assert fetched[0] == "https://raw.githubusercontent.com/mamamou/ai-coding-skills/main/code-reviewer/SKILL.md"


def test_non_github_registry_url_is_resolved_before_direct_download(tmp_path, monkeypatch):
    monkeypatch.setenv("MATTED_SKILL_WEB_ALLOWED_HOSTS", "skills.example.com")
    master = make_master(tmp_path)
    calls = []

    def fake_registry(skill_ref):
        calls.append(("registry", skill_ref))
        cache_dir = tmp_path / "Playbooks" / ".cache" / "code-reviewer"
        cache_dir.mkdir(parents=True)
        return cache_dir, "Skill from registry page."

    def fake_direct(skill_ref):
        calls.append(("direct", skill_ref))
        return None

    monkeypatch.setattr(master, "_fetch_skill_from_registry_url", fake_registry)
    monkeypatch.setattr(master, "_download_direct_skill_reference", fake_direct)

    prompt, loaded = master._augment_skill_prompt("skill: https://skills.example.com/code-reviewer")

    assert loaded == ["Playbooks/.cache/code-reviewer"]
    assert "Skill from registry page." in prompt
    assert calls == [("registry", "https://skills.example.com/code-reviewer")]
