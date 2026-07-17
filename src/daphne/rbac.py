import asyncio
import os
import time
import logging
from typing import Dict, Any, Tuple, Optional
from daphne.config import rbac_config, rbac_valkey_url

try:
    import valkey.asyncio as valkey_asyncio
except ImportError:
    valkey_asyncio = None

logger = logging.getLogger(__name__)

# Valkey key layout for live RBAC edits. Namespaced by app so other
# self-hosted bots (e.g. suzuran) can share the same Valkey instance.
VALKEY_ROLES_KEY = "daphne:rbac:roles"
VALKEY_USERS_KEY = "daphne:rbac:users"
VALKEY_CHATS_KEY = "daphne:rbac:chats"
VALKEY_PUBLIC_COMMANDS_KEY = "daphne:rbac:public_commands"


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
    def __init__(
        self,
        config_dict: Optional[Dict[str, Any]] = None,
        valkey_url: Optional[str] = None,
    ):
        if config_dict is None:
            config_dict = {}
        self.valkey_url = valkey_url

        # Load public commands
        self.public_commands = set(
            c.lower() for c in config_dict.get("public_commands", ["help"])
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

        try:
            self.convert_link_limit = int(config_dict.get("convert_link_limit", 60))
        except (TypeError, ValueError):
            self.convert_link_limit = 60

        try:
            self.extract_audio_limit = int(config_dict.get("extract_audio_limit", 10))
        except (TypeError, ValueError):
            self.extract_audio_limit = 10

        try:
            self.preview_video_limit = int(config_dict.get("preview_video_limit", 10))
        except (TypeError, ValueError):
            self.preview_video_limit = 10

        try:
            self.download_video_limit = int(config_dict.get("download_video_limit", 5))
        except (TypeError, ValueError):
            self.download_video_limit = 5

        try:
            self.fetch_metadata_limit = int(config_dict.get("fetch_metadata_limit", 30))
        except (TypeError, ValueError):
            self.fetch_metadata_limit = 30

        try:
            self.help_limit = int(config_dict.get("help_limit", 10))
        except (TypeError, ValueError):
            self.help_limit = 10

        self.action_timestamps: Dict[Tuple[int, str], list[float]] = {}

    @classmethod
    def load(cls, path: str | None = None) -> "RbacService":
        valkey_url = rbac_valkey_url()

        config_dict = rbac_config()
        if config_dict:
            logger.info("RBAC configuration loaded from config.toml")
            return cls(config_dict, valkey_url=valkey_url)

        if path is None:
            path = get_rbac_config_path()
        if not os.path.exists(path):
            logger.warning(
                f"RBAC configuration file not found at {path}. Falling back to default configuration."
            )
            return cls(valkey_url=valkey_url)

        try:
            import tomllib

            with open(path, "rb") as f:
                data = tomllib.load(f)
            logger.info(f"RBAC configuration loaded from {path}")
            return cls(data, valkey_url=valkey_url)
        except Exception as e:
            logger.error(
                f"Failed to load RBAC configuration from {path}: {e}. Falling back to default configuration."
            )
            return cls(valkey_url=valkey_url)

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

    def check_access(
        self, user_id: int, chat_id: int, command: str, dry_run: bool = False
    ) -> AccessResult:
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
            quota_res = self._check_and_record_quota(user_id, command, dry_run)
            if not quota_res.is_allowed():
                return quota_res

            now = time.time()
            limit_key = (user_id, command)

            if limit_key in self.rate_limiter:
                count, start_time = self.rate_limiter[limit_key]
                if now - start_time >= 60.0:
                    if not dry_run:
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

                if not dry_run:
                    self.rate_limiter[limit_key] = (count + 1, start_time)
                logger.info(
                    f"RBAC: [ALLOWED] public command (count={count + 1}) for user={user_id} cmd={command}"
                )
                return AccessResult(AccessStatus.ALLOWED)
            else:
                if not dry_run:
                    self.rate_limiter[limit_key] = (1, now)
                logger.info(
                    f"RBAC: [ALLOWED] public command (first request) for user={user_id} cmd={command}"
                )
                return AccessResult(AccessStatus.ALLOWED)

        # 3. Chat Whitelist Enforcement
        chat_role = self.chats.get(chat_id)
        if not chat_role:
            logger.warning(
                f"RBAC: [DENIED] chat not in whitelist: chat={chat_id} cmd={command}"
            )
            return AccessResult(AccessStatus.DENIED, "Chat not in whitelist")

        # 4. Check Chat Permission (standard/group permission fallback)
        chat_allowed = self.has_permission(chat_role, command)
        if chat_allowed:
            quota_res = self._check_and_record_quota(user_id, command, dry_run)
            if not quota_res.is_allowed():
                return quota_res
            logger.info(
                f"RBAC: [ALLOWED] chat-level access for user={user_id} chat={chat_id} (role={chat_role}) cmd={command}"
            )
            return AccessResult(AccessStatus.ALLOWED)

        # 5. Check User Permission (if chat role does not have permission, but user role does)
        if user_role:
            user_allowed = self.has_permission(user_role, command)
            if user_allowed:
                quota_res = self._check_and_record_quota(user_id, command, dry_run)
                if not quota_res.is_allowed():
                    return quota_res
                logger.info(
                    f"RBAC: [ALLOWED] user-level access for user={user_id} (role={user_role}) in chat={chat_id} cmd={command}"
                )
                return AccessResult(AccessStatus.ALLOWED)
            else:
                logger.warning(
                    f"RBAC: [DENIED] user role lacks permission: user={user_id} (role={user_role}) cmd={command}"
                )
        else:
            logger.warning(
                f"RBAC: [DENIED] user not whitelisted and chat role lacks permission: user={user_id} chat={chat_id} cmd={command}"
            )

        return AccessResult(
            AccessStatus.DENIED, "Command not allowed by user or chat role"
        )

    def _check_and_record_quota(
        self, user_id: int, command: str, dry_run: bool = False
    ) -> AccessResult:
        # Define limits for actions
        limits = {
            "convert_link": self.convert_link_limit,
            "extract_audio": self.extract_audio_limit,
            "download_video": self.download_video_limit,
            "preview_video": self.preview_video_limit,
            "fetch_metadata": self.fetch_metadata_limit,
            "help": self.help_limit,
        }

        if command not in limits:
            return AccessResult(AccessStatus.ALLOWED)

        limit = limits[command]
        now = time.time()
        one_hour_ago = now - 3600.0

        key = (user_id, command)
        timestamps = self.action_timestamps.get(key, [])
        # Clean up old timestamps
        active_timestamps = [t for t in timestamps if t > one_hour_ago]

        if len(active_timestamps) >= limit:
            logger.warning(
                f"RBAC: [RATE_LIMITED] {command} quota exceeded for user={user_id} (limit={limit}/hr)"
            )
            friendly_name = command.replace("_", " ")
            return AccessResult(
                AccessStatus.RATE_LIMITED,
                f"{friendly_name.capitalize()} hourly quota exceeded",
            )

        if not dry_run:
            active_timestamps.append(now)
            self.action_timestamps[key] = active_timestamps
        return AccessResult(AccessStatus.ALLOWED)

    def is_admin(self, user_id: int) -> bool:
        return self.users.get(user_id) == "admin"

    def role_exists(self, role: str) -> bool:
        return role in self.roles

    def list_roles(self) -> list[tuple[str, list[str]]]:
        return sorted((name, sorted(perms)) for name, perms in self.roles.items())

    def grant_user(self, user_id: int, role: str) -> None:
        self.users[str(user_id)] = role
        self.users[user_id] = role

    def revoke_user(self, user_id: int) -> bool:
        existed = user_id in self.users or str(user_id) in self.users
        self.users.pop(user_id, None)
        self.users.pop(str(user_id), None)
        return existed

    def grant_chat(self, chat_id: int, role: str) -> None:
        self.chats[str(chat_id)] = role
        self.chats[chat_id] = role

    def revoke_chat(self, chat_id: int) -> bool:
        existed = chat_id in self.chats or str(chat_id) in self.chats
        self.chats.pop(chat_id, None)
        self.chats.pop(str(chat_id), None)
        return existed


def get_rbac_config_path() -> str:
    if os.path.exists("rbac.toml"):
        return "rbac.toml"
    return os.path.expanduser("~/.config/daphne/rbac.toml")


async def refresh_rbac_from_valkey(rbac: RbacService) -> bool:
    """Re-reads roles/users/chats from Valkey, replacing the in-memory RBAC state.

    Returns True on a successful refresh. On an empty store (first boot) this
    seeds Valkey from the current in-memory config instead of wiping RBAC, and
    returns False. On a connection error, the previous state is kept as-is.
    """
    if not rbac.valkey_url or valkey_asyncio is None:
        return False

    try:
        client = valkey_asyncio.Redis.from_url(
            rbac.valkey_url, decode_responses=True, socket_timeout=5
        )
        try:
            roles_raw = await client.hgetall(VALKEY_ROLES_KEY)
            users_raw = await client.hgetall(VALKEY_USERS_KEY)
            chats_raw = await client.hgetall(VALKEY_CHATS_KEY)
            public_raw = await client.smembers(VALKEY_PUBLIC_COMMANDS_KEY)
        finally:
            await client.aclose()
    except Exception as e:
        logger.warning("Valkey RBAC refresh failed; keeping previous config: %s", e)
        return False

    if not roles_raw and not users_raw and not chats_raw:
        await seed_valkey_from_rbac(rbac)
        return False

    roles = {
        name: set(p.lower() for p in perms.split(",") if p)
        for name, perms in roles_raw.items()
    }

    users: Dict[Any, str] = {}
    for k, v in users_raw.items():
        users[str(k)] = v
        try:
            users[int(k)] = v
        except ValueError:
            pass

    chats: Dict[Any, str] = {}
    for k, v in chats_raw.items():
        chats[str(k)] = v
        try:
            chats[int(k)] = v
        except ValueError:
            pass

    rbac.roles = roles
    rbac.users = users
    rbac.chats = chats
    if public_raw:
        rbac.public_commands = set(c.lower() for c in public_raw)
    logger.info(
        "RBAC refreshed from Valkey; roles=%d users=%d chats=%d",
        len(roles),
        len(users_raw),
        len(chats_raw),
    )
    return True


async def seed_valkey_from_rbac(rbac: RbacService) -> None:
    """One-time bootstrap: copies the statically-configured RBAC into Valkey
    so it becomes the live, editable source of truth from then on."""
    if not rbac.valkey_url or valkey_asyncio is None:
        return
    try:
        client = valkey_asyncio.Redis.from_url(
            rbac.valkey_url, decode_responses=True, socket_timeout=5
        )
        try:
            for role_name, perms in rbac.roles.items():
                await client.hset(VALKEY_ROLES_KEY, role_name, ",".join(sorted(perms)))
            for user_id, role in rbac.users.items():
                if isinstance(user_id, int):
                    await client.hset(VALKEY_USERS_KEY, str(user_id), role)
            for chat_id, role in rbac.chats.items():
                if isinstance(chat_id, int):
                    await client.hset(VALKEY_CHATS_KEY, str(chat_id), role)
            if rbac.public_commands:
                await client.sadd(VALKEY_PUBLIC_COMMANDS_KEY, *rbac.public_commands)
        finally:
            await client.aclose()
        logger.info("Seeded Valkey RBAC store from config.toml (first boot).")
    except Exception as e:
        logger.warning("Failed to seed Valkey RBAC store: %s", e)


async def persist_grant_to_valkey(
    rbac: RbacService, key: str, field: str, value: str
) -> bool:
    if not rbac.valkey_url or valkey_asyncio is None:
        return False
    try:
        client = valkey_asyncio.Redis.from_url(
            rbac.valkey_url, decode_responses=True, socket_timeout=5
        )
        try:
            await client.hset(key, field, value)
        finally:
            await client.aclose()
        return True
    except Exception as e:
        logger.warning("Failed to persist RBAC grant to Valkey: %s", e)
        return False


async def persist_revoke_to_valkey(rbac: RbacService, key: str, field: str) -> bool:
    if not rbac.valkey_url or valkey_asyncio is None:
        return False
    try:
        client = valkey_asyncio.Redis.from_url(
            rbac.valkey_url, decode_responses=True, socket_timeout=5
        )
        try:
            await client.hdel(key, field)
        finally:
            await client.aclose()
        return True
    except Exception as e:
        logger.warning("Failed to persist RBAC revoke to Valkey: %s", e)
        return False


async def valkey_rbac_refresh_loop(rbac: RbacService, interval: float = 30.0) -> None:
    """Background task: periodically re-syncs RBAC state from Valkey."""
    while True:
        await asyncio.sleep(interval)
        await refresh_rbac_from_valkey(rbac)
