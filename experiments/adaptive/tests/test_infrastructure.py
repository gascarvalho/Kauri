from pathlib import Path


def test_scaffold_uses_an_isolated_run_directory(
    repository_root: Path,
    run_directory: Path,
    deterministic_seed: int,
) -> None:
    assert (repository_root / "CMakeLists.txt").is_file()
    assert run_directory.is_dir()
    assert repository_root not in run_directory.parents
    assert deterministic_seed == 1729
