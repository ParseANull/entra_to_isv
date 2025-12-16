from entra_to_isv.config import DEFAULT_MAPPING
from entra_to_isv.mapper import map_user


def test_map_user_basic_includes_defaults_and_mobile():
    source = {
        "userPrincipalName": "alice@example.com",
        "givenName": "Alice",
        "surname": "Anderson",
        "displayName": "Alice A",
        "mail": "alice@example.com",
        "accountEnabled": True,
        "mobilePhone": "+1-555-1111",
    }

    mapped = map_user(source, DEFAULT_MAPPING, include_mobile=True)

    assert mapped["userName"] == "alice@example.com"
    assert mapped["name"]["givenName"] == "Alice"
    assert mapped["name"]["familyName"] == "Anderson"
    assert mapped["displayName"] == "Alice A"
    assert mapped["active"] is True

    emails = mapped.get("emails")
    assert emails and emails[0]["value"] == "alice@example.com"
    assert mapped.get("phoneNumbers") == [
        {"value": "+1-555-1111", "type": "mobile", "primary": True}
    ]


def test_map_user_skips_missing_email():
    source = {
        "userPrincipalName": "bob@example.com",
        "givenName": "Bob",
        "surname": "Baker",
        "displayName": "Bob B",
        "accountEnabled": False,
    }

    mapped = map_user(source, DEFAULT_MAPPING)

    assert "emails" not in mapped
    assert mapped["active"] is False
