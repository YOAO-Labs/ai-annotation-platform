from aap_identity.password_policy import MIN_LENGTH, validate_password_strength


def test_password_policy_keeps_existing_api_strength_messages_and_minimum():
    assert MIN_LENGTH == 8
    assert validate_password_strength("short") == [
        "密码长度至少 8 位",
        "密码需包含大写字母",
        "密码需包含数字",
    ]
    assert validate_password_strength("StrongPass1") == []
