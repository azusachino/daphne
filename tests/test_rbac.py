import unittest

from daphne.rbac import RbacService


def make_rbac():
    return RbacService(
        {
            "roles": {
                "admin": {"permissions": ["*"]},
                "standard_group": {"permissions": ["convert_link"]},
            },
            "users": {111: "admin"},
            "chats": {-100: "standard_group"},
        }
    )


class TestRbacService(unittest.TestCase):
    def test_is_admin(self):
        rbac = make_rbac()
        self.assertTrue(rbac.is_admin(111))
        self.assertFalse(rbac.is_admin(222))


if __name__ == "__main__":
    unittest.main()
