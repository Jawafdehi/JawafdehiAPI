"""Import-smoke for the registry + command wiring (surfaces import errors)."""
from courts.scraper import registry


def test_registry_has_all_four_courts():
    assert set(registry.REGISTRY) == {"special", "district", "high", "supreme"}


def test_resolve_all_and_single_and_unknown():
    assert set(registry.resolve("all")) == {"special", "district", "high", "supreme"}
    assert registry.resolve("special") == ["special"]
    import pytest
    with pytest.raises(KeyError):
        registry.resolve("bogus")


def test_court_ids_counts():
    assert registry.REGISTRY["supreme"].court_ids(None) == ["supreme"]
    assert len(registry.REGISTRY["district"].court_ids(None)) >= 70
    assert len(registry.REGISTRY["high"].court_ids(None)) >= 15


def test_command_imports():
    from courts.management.commands.scrape_courtcases import Command
    assert Command.help
