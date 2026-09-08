from kosell_bot.messages import credentials_message, duration_text, steam_code_message


def test_duration_text():
    assert duration_text(24) == "1 день"
    assert duration_text(72) == "3 дня"
    assert duration_text(168) == "7 дней"
    assert duration_text(12) == "12 ч."


def test_credentials_message_contains_guard_instruction():
    text = credentials_message(
        product_name="Game",
        duration_hours=24,
        login="steam-login",
        password="steam-password",
        expires_at="2030-01-01T00:00:00+00:00",
        timezone_name="Europe/Moscow",
    )
    assert "напишите команду: !код" in text
    assert "снова напишите: !код" in text
    assert "steam-login" in text
    assert "steam-password" in text


def test_steam_code_message():
    text = steam_code_message("ABCDE", 18, "Game")
    assert "ABCDE" in text
    assert "18 сек" in text
    assert "снова напишите: !код" in text
