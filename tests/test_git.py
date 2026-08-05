from lean_runtime._git import git_command


def test_git_commands_enable_windows_long_paths_without_global_configuration() -> None:
    assert git_command("clone", "source", "destination") == [
        "git",
        "-c",
        "core.longpaths=true",
        "clone",
        "source",
        "destination",
    ]
