from ai_code_change_agent.application.git_providers.ports import (
    GitProviderRegistry,
    ProviderRepositoryInfo,
)


class Fake:
    async def describe_repository(
        self, *, owner: str, name: str, access_reference: str
    ) -> ProviderRepositoryInfo:
        return ProviderRepositoryInfo("1", owner, name, "main")


def test_registry_is_a_plain_key_to_adapter_map() -> None:
    reg = GitProviderRegistry()
    assert not reg.keys and reg.get("any") is None
    reg.register("zeta", Fake())
    reg.register("alpha", Fake())
    assert reg.keys == ("alpha", "zeta") and reg.get("zeta") is not None
    assert GitProviderRegistry({"x": Fake()}).keys == ("x",)
