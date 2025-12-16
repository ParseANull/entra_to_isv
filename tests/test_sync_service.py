from entra_to_isv.sync_service import _needs_update


def _user(payload_overrides=None):
    payload = {
        "userName": "carol@example.com",
        "displayName": "Carol C",
        "active": True,
        "name": {"givenName": "Carol", "familyName": "Clark"},
        "emails": [
            {"value": "carol@example.com", "type": "work", "primary": True},
        ],
        "phoneNumbers": [],
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return payload


def test_needs_update_false_when_same_core_fields():
    mapped = _user()
    existing = _user()
    assert _needs_update(mapped, existing) is False


def test_needs_update_detects_display_name_change():
    mapped = _user({"displayName": "Carol Changed"})
    existing = _user()
    assert _needs_update(mapped, existing) is True


def test_needs_update_ignores_email_order():
    mapped = _user(
        {
            "emails": [
                {"value": "carol+work@example.com", "type": "work"},
                {"value": "carol@example.com", "type": "home"},
            ]
        }
    )
    existing = _user(
        {
            "emails": [
                {"value": "carol@example.com", "type": "home"},
                {"value": "carol+work@example.com", "type": "work"},
            ]
        }
    )
    assert _needs_update(mapped, existing) is False


def test_needs_update_detects_phone_change():
    mapped = _user({"phoneNumbers": [{"value": "+1-555-2222", "type": "mobile"}]})
    existing = _user({"phoneNumbers": [{"value": "+1-555-1111", "type": "mobile"}]})
    assert _needs_update(mapped, existing) is True
