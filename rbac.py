import os
import time
import logging
import tomllib
from typing import Dict, Any, Tuple, Optional

logger = logging.getLogger(__name__)


class AccessStatus:
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
    RATE_LIMITED = "RATE_LIMITED"


class AccessResult:
    def __init__(self, status: str, reason: str = ""):
        self.status = status
        self.reason = reason

    def is_allowed(self) -> bool:
        return self.status == AccessStatus.ALLOWED

    def is_rate_limited(self) -> bool:
        return self.status == AccessStatus.RATE_LIMITED

    def is_denied(self) -> bool:
        return self.status == AccessStatus.DENIED


class RbacService:
    def __init__(self, config_dict: Optional[Dict[str, Any]] = None):
        if config_dict is None:
            config_dict = {}

        # Load public commands
        self.public_commands = set(
            c.lower() for c in config_dict.get("public_commands", ["rate", "help"])
        )

        # Load roles
        self.roles = {}
        roles_data = config_dict.get("roles", {})
        for role_name, role_info in roles_data.items():
            perms = set(p.lower() for p in role_info.get("permissions", []))
            self.roles[role_name] = perms

        # Load users and chats. Store keys as both int and str to be absolutely safe
        self.users = {}
        for k, v in config_dict.get("users", {}).items():
            self.users[str(k)] = v
            try:
                self.users[int(k)] = v
            except ValueError:
                pass

        self.chats = {}
        for k, v in config_dict.get("chats", {}).items():
            self.chats[str(k)] = v
            try:
                self.chats[int(k)] = v
            except ValueError:
                pass

        # Rate limiter: (user_id, command) -> (count, start_time)
        self.rate_limiter: Dict[Tuple[int, str], Tuple[int, float]] = {}

    @classmethod
    def load(cls, path: str) -> "RbacService":
        if not os.path.exists(path):
            logger.warning(
                f"RBAC configuration file not found at {path}. Falling back to default configuration."
            )
            return cls()

        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
            logger.info(f"RBAC configuration loaded from {path}")
            return cls(data)
        except Exception as e:
            logger.error(
                f"Failed to load RBAC configuration from {path}: {e}. Falling back to default configuration."
            )
            return cls()

    def has_permission(self, role_name: str, command: str) -> bool:
        perms = self.roles.get(role_name)
        if not perms:
            return False
        return "*" in perms or command in perms

    def check_access_chat(self, chat_id: int, command: str) -> AccessResult:
        command = command.lower().lstrip("/")
        chat_role = self.chats.get(chat_id)
        if not chat_role:
            logger.warning(
                f"RBAC: [DENIED] chat not in whitelist: chat={chat_id} cmd={command}"
            )
            return AccessResult(AccessStatus.DENIED, "Chat not in whitelist")

        if self.has_permission(chat_role, command):
            logger.info(
                f"RBAC: [ALLOWED] chat-level access for chat={chat_id} cmd={command}"
            )
            return AccessResult(AccessStatus.ALLOWED)
        else:
            logger.warning(
                f"RBAC: [DENIED] chat role lacks permission: chat={chat_id} cmd={command}"
            )
            return AccessResult(AccessStatus.DENIED, "Command not allowed by chat role")

    def check_access(self, user_id: int, chat_id: int, command: str) -> AccessResult:
        command = command.lower().lstrip("/")

        # 1. Admin Bypass
        user_role = self.users.get(user_id)
        if user_role == "admin":
            logger.info(
                f"RBAC: [ALLOWED] admin bypass for user={user_id} cmd={command}"
            )
            return AccessResult(AccessStatus.ALLOWED)

        # 2. Public Command Check
        if command in self.public_commands:
            now = time.time()
            limit_key = (user_id, command)

            if limit_key in self.rate_limiter:
                count, start_time = self.rate_limiter[limit_key]
                if now - start_time >= 60.0:
                    self.rate_limiter[limit_key] = (1, now)
                    logger.info(
                        f"RBAC: [ALLOWED] public command (limit reset) for user={user_id} cmd={command}"
                    )
                    return AccessResult(AccessStatus.ALLOWED)

                if count >= 10:
                    logger.warning(
                        f"RBAC: [RATE_LIMITED] public command for user={user_id} cmd={command}"
                    )
                    return AccessResult(AccessStatus.RATE_LIMITED)

                self.rate_limiter[limit_key] = (count + 1, start_time)
                logger.info(
                    f"RBAC: [ALLOWED] public command (count={count + 1}) for user={user_id} cmd={command}"
                )
                return AccessResult(AccessStatus.ALLOWED)
            else:
                self.rate_limiter[limit_key] = (1, now)
                logger.info(
                    f"RBAC: [ALLOWED] public command (first request) for user={user_id} cmd={command}"
                )
                return AccessResult(AccessStatus.ALLOWED)

        # 3. Whitelist Enforcement
        if not user_role:
            logger.warning(
                f"RBAC: [DENIED] user not in whitelist: user={user_id} cmd={command}"
            )
            return AccessResult(AccessStatus.DENIED, "User not in whitelist")

        chat_role = self.chats.get(chat_id)
        if not chat_role:
            logger.warning(
                f"RBAC: [DENIED] chat not in whitelist: chat={chat_id} cmd={command}"
            )
            return AccessResult(AccessStatus.DENIED, "Chat not in whitelist")

        # 4. Intersection Logic
        user_allowed = self.has_permission(user_role, command)
        chat_allowed = self.has_permission(chat_role, command)

        if user_allowed and chat_allowed:
            logger.info(
                f"RBAC: [ALLOWED] intersection for user={user_id} chat={chat_id} cmd={command}"
            )
            return AccessResult(AccessStatus.ALLOWED)
        else:
            logger.warning(
                f"RBAC: [DENIED] intersection failed for user={user_id} (allowed={user_allowed}) chat={chat_id} (allowed={chat_allowed}) cmd={command}"
            )
            return AccessResult(
                AccessStatus.DENIED, "Command not allowed by user or chat role"
            )


def get_rbac_config_path() -> str:
    path = os.environ.get("RBAC_CONFIG_PATH")
    if path:
        return path
    if os.path.exists("rbac.toml"):
        return "rbac.toml"
    return os.path.expanduser("~/.config/daphne/rbac.toml")
