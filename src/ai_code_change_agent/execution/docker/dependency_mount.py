"""The read-only dependency-bundle mount shared by agent sandboxes and validation containers."""

DEPENDENCY_MOUNT = "/opt/deps"
_PATH = f"{DEPENDENCY_MOUNT}/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"


def dependency_args(dependency_dir: str | None) -> list[str]:
    """Docker arguments for a verified bundle (empty when there is none): mounted read-only, put
    on PYTHONPATH and PATH. Nothing is installed inside the sandbox and nothing is fetched."""
    if dependency_dir is None:
        return []
    return [
        "--mount",
        f"type=bind,src={dependency_dir},dst={DEPENDENCY_MOUNT},readonly",
        "--env",
        f"PYTHONPATH={DEPENDENCY_MOUNT}",
        "--env",
        f"PATH={_PATH}",
    ]
