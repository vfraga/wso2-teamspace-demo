from agent.gemini_agent import t, MESSAGES, _meeting_list_item


def test_t_returns_localized_string():
    assert t("en", "greeting") == MESSAGES["greeting"]["en"]
    assert t("pt", "greeting") == MESSAGES["greeting"]["pt"]


def test_t_falls_back_to_english_for_unknown_language():
    assert t("fr", "greeting") == MESSAGES["greeting"]["en"]


def test_t_formats_kwargs():
    result = t("en", "delete_failed", message="network down")
    assert "network down" in result
    assert result == MESSAGES["delete_failed"]["en"].format(message="network down")


def test_t_returns_empty_for_unknown_key():
    assert t("en", "nonexistent_key_xyz") == ""


def test_meeting_list_item_uses_language():
    meeting = {"id": "m1", "topic": "Planning", "date": "2026-06-10", "start_time": "15:00"}
    en = _meeting_list_item("en", meeting)
    pt = _meeting_list_item("pt", meeting)
    assert "at 15:00" in en
    assert "às 15:00" in pt
    assert en != pt
