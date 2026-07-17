import unittest
from unittest.mock import AsyncMock, patch

from daphne.rbac import (
    RbacService,
    refresh_rbac_from_valkey,
    seed_valkey_from_rbac,
    persist_grant_to_valkey,
    persist_revoke_to_valkey,
)


def make_rbac(valkey_url=None):
    return RbacService(
        {
            "roles": {
                "admin": {"permissions": ["*"]},
                "standard_group": {"permissions": ["convert_link"]},
            },
            "users": {111: "admin"},
            "chats": {-100: "standard_group"},
        },
        valkey_url=valkey_url,
    )


class TestRbacGrantRevoke(unittest.TestCase):
    def test_is_admin(self):
        rbac = make_rbac()
        self.assertTrue(rbac.is_admin(111))
        self.assertFalse(rbac.is_admin(222))

    def test_role_exists_and_list_roles(self):
        rbac = make_rbac()
        self.assertTrue(rbac.role_exists("standard_group"))
        self.assertFalse(rbac.role_exists("nope"))
        names = [name for name, _ in rbac.list_roles()]
        self.assertEqual(names, ["admin", "standard_group"])

    def test_grant_and_revoke_user(self):
        rbac = make_rbac()
        rbac.grant_user(222, "standard_group")
        self.assertEqual(rbac.users[222], "standard_group")
        self.assertEqual(rbac.users["222"], "standard_group")

        self.assertTrue(rbac.revoke_user(222))
        self.assertNotIn(222, rbac.users)
        self.assertNotIn("222", rbac.users)
        self.assertFalse(rbac.revoke_user(222))

    def test_grant_and_revoke_chat(self):
        rbac = make_rbac()
        rbac.grant_chat(-200, "standard_group")
        self.assertEqual(rbac.chats[-200], "standard_group")

        self.assertTrue(rbac.revoke_chat(-200))
        self.assertNotIn(-200, rbac.chats)
        self.assertFalse(rbac.revoke_chat(-200))


class TestValkeySync(unittest.IsolatedAsyncioTestCase):
    def _mock_client(self, **hgetall_returns):
        client = AsyncMock()
        client.hgetall = AsyncMock(side_effect=lambda key: hgetall_returns.get(key, {}))
        client.smembers = AsyncMock(return_value=set())
        client.aclose = AsyncMock()
        client.hset = AsyncMock()
        client.hdel = AsyncMock()
        client.sadd = AsyncMock()
        return client

    async def test_refresh_without_valkey_url_is_noop(self):
        rbac = make_rbac(valkey_url=None)
        result = await refresh_rbac_from_valkey(rbac)
        self.assertFalse(result)

    async def test_refresh_replaces_state_from_valkey(self):
        rbac = make_rbac(valkey_url="redis://valkey:6379/0")
        client = self._mock_client(
            **{
                "daphne:rbac:roles": {"admin": "*"},
                "daphne:rbac:users": {"333": "admin"},
                "daphne:rbac:chats": {"-300": "admin"},
            }
        )
        with patch("daphne.rbac.valkey_asyncio") as mock_module:
            mock_module.Redis.from_url.return_value = client
            result = await refresh_rbac_from_valkey(rbac)

        self.assertTrue(result)
        self.assertEqual(rbac.roles["admin"], {"*"})
        self.assertEqual(rbac.users[333], "admin")
        self.assertEqual(rbac.chats[-300], "admin")

    async def test_refresh_on_empty_store_seeds_instead_of_wiping(self):
        rbac = make_rbac(valkey_url="redis://valkey:6379/0")
        client = self._mock_client()
        with patch("daphne.rbac.valkey_asyncio") as mock_module:
            mock_module.Redis.from_url.return_value = client
            result = await refresh_rbac_from_valkey(rbac)

        self.assertFalse(result)
        # Original config-loaded state must survive an empty Valkey store.
        self.assertTrue(rbac.is_admin(111))
        client.hset.assert_any_call("daphne:rbac:roles", "admin", "*")

    async def test_refresh_connection_error_keeps_previous_state(self):
        rbac = make_rbac(valkey_url="redis://valkey:6379/0")
        with patch("daphne.rbac.valkey_asyncio") as mock_module:
            mock_module.Redis.from_url.side_effect = ConnectionError("boom")
            result = await refresh_rbac_from_valkey(rbac)

        self.assertFalse(result)
        self.assertTrue(rbac.is_admin(111))

    async def test_seed_writes_current_state_to_valkey(self):
        rbac = make_rbac(valkey_url="redis://valkey:6379/0")
        client = self._mock_client()
        with patch("daphne.rbac.valkey_asyncio") as mock_module:
            mock_module.Redis.from_url.return_value = client
            await seed_valkey_from_rbac(rbac)

        client.hset.assert_any_call("daphne:rbac:users", "111", "admin")
        client.hset.assert_any_call("daphne:rbac:chats", "-100", "standard_group")

    async def test_persist_grant_and_revoke(self):
        rbac = make_rbac(valkey_url="redis://valkey:6379/0")
        client = self._mock_client()
        with patch("daphne.rbac.valkey_asyncio") as mock_module:
            mock_module.Redis.from_url.return_value = client
            granted = await persist_grant_to_valkey(
                rbac, "daphne:rbac:users", "222", "standard_group"
            )
            revoked = await persist_revoke_to_valkey(rbac, "daphne:rbac:users", "222")

        self.assertTrue(granted)
        self.assertTrue(revoked)
        client.hset.assert_called_once_with(
            "daphne:rbac:users", "222", "standard_group"
        )
        client.hdel.assert_called_once_with("daphne:rbac:users", "222")


if __name__ == "__main__":
    unittest.main()
