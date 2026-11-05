"""MVP owner check: only OWNER_ID may open /admin. No role system in MVP."""


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeMessage:
    def __init__(self, user_id):
        self.from_user = FakeUser(user_id)


def test_owner_check():
    from app.bot.handlers import is_owner
    from app.config import Config

    cfg = Config(bot_token="", owner_id=123)
    # Config reads OWNER_ID from env in __post_init__; set explicitly here.
    cfg.owner_id = 123
    assert is_owner(FakeMessage(123), cfg)
    assert not is_owner(FakeMessage(999), cfg)
    cfg.owner_id = None
    assert not is_owner(FakeMessage(123), cfg)
