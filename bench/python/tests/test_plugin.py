from pathlib import Path


def test_recipe_skips_without_opt_in(pytester, monkeypatch):
    monkeypatch.delenv("BENCH_CLIENT_TOKEN", raising=False)
    pytester.makepyfile('def test_no_hardware_by_default(board): pass')
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(skipped=1)


def test_recipe_fails_without_credentials(pytester, monkeypatch):
    monkeypatch.delenv("BENCH_CLIENT_TOKEN", raising=False)
    pytester.makepyfile('def test_missing_credentials(board): pass')
    result = pytester.runpytest_subprocess("--bench", "-q")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*Set BENCH_CLIENT_TOKEN*"])


def test_actual_recipe_runs_through_api_and_exports_proofs(pytester, monkeypatch, sim_bench):
    monkeypatch.setenv("BENCH_CLIENT_TOKEN", "pytest-client-test-token")
    recipe = Path(__file__).resolve().parents[2] / "recipes/test_read_only.py"
    pytester.makepyfile(test_recipe=recipe.read_text())
    result = pytester.runpytest_subprocess("--bench", "--bench-url", sim_bench.url, "--bench-device", "board",
        "--bench-results", str(pytester.path / "proofs"), "--junitxml=" + str(pytester.path / "report.xml"), "-q")
    result.assert_outcomes(passed=1)
    assert sim_bench.device("board")["reservation"] is None
    assert len(list((pytester.path / "proofs").rglob("*.bin"))) == 2
    assert len(list((pytester.path / "proofs").rglob("recipe.py"))) == 1
    assert (pytester.path / "report.xml").exists()


def test_failed_assertion_still_releases_reservation(pytester, monkeypatch, sim_bench):
    monkeypatch.setenv("BENCH_CLIENT_TOKEN", "pytest-client-test-token")
    pytester.makepyfile('def test_failure(board):\n    board.identify()\n    assert False, "deliberate recipe failure"')
    result = pytester.runpytest_subprocess("--bench", "--bench-url", sim_bench.url, "--bench-device", "board", "-q")
    result.assert_outcomes(failed=1)
    assert sim_bench.device("board")["reservation"] is None
