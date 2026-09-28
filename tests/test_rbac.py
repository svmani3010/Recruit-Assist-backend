"""RBAC dual-path scaffold tests (bootstrap, matrix, session, audit)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import MagicMock, patch

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.rbac import constants as C
from app.rbac.bootstrap import resolve_org_on_login
from app.rbac.org_service import is_consumer_domain
from app.rbac.permissions import can
from app.rbac.seed_matrix import _RECRUITMENT_ROWS


class _FakeCollection:
    def __init__(self, name: str, store: dict[str, list], unique_keys: Optional[list] = None):
        self.name = name
        self._store = store
        self._unique_keys = unique_keys or []
        self._store.setdefault(name, [])

    def _docs(self) -> list:
        return self._store[self.name]

    def find_one(self, query: dict, projection: Optional[dict] = None):
        for doc in self._docs():
            if self._match(doc, query):
                return dict(doc)
        return None

    def find(self, query: dict, projection: Optional[dict] = None):
        return [dict(d) for d in self._docs() if self._match(d, query)]

    def insert_one(self, doc: dict):
        doc = dict(doc)
        if "_id" not in doc:
            doc["_id"] = ObjectId()
        for keys in self._unique_keys:
            q = {k: doc.get(k) for k in keys}
            if self.find_one(q):
                raise DuplicateKeyError("E11000 duplicate key")
        self._docs().append(doc)
        result = MagicMock()
        result.inserted_id = doc["_id"]
        return result

    def insert_many(self, docs: list):
        for d in docs:
            self.insert_one(d)

    def update_one(self, query: dict, update: dict, upsert: bool = False):
        doc = self.find_one(query)
        if doc is None:
            if not upsert:
                result = MagicMock()
                result.modified_count = 0
                return result
            new_doc: dict[str, Any] = {}
            if "$setOnInsert" in update:
                new_doc.update(update["$setOnInsert"])
            if "$set" in update:
                new_doc.update(update["$set"])
            # merge query equality fields
            for k, v in query.items():
                if not str(k).startswith("$"):
                    new_doc.setdefault(k, v)
            self.insert_one(new_doc)
            result = MagicMock()
            result.modified_count = 1
            return result
        idx = next(i for i, d in enumerate(self._docs()) if d["_id"] == doc["_id"])
        if "$set" in update:
            self._docs()[idx].update(update["$set"])
        result = MagicMock()
        result.modified_count = 1
        return result

    def delete_one(self, query: dict):
        before = len(self._docs())
        self._store[self.name] = [
            d for d in self._docs() if not self._match(d, query)
        ]
        result = MagicMock()
        result.deleted_count = before - len(self._docs())
        return result

    def delete_many(self, query: dict):
        before = len(self._docs())
        self._store[self.name] = [
            d for d in self._docs() if not self._match(d, query)
        ]
        result = MagicMock()
        result.deleted_count = before - len(self._docs())
        return result

    def create_index(self, *args, **kwargs):
        return "idx"

    @staticmethod
    def _match(doc: dict, query: dict) -> bool:
        for k, v in query.items():
            if k == "$or":
                if not any(_FakeCollection._match(doc, clause) for clause in v):
                    return False
                continue
            if k == "$and":
                if not all(_FakeCollection._match(doc, clause) for clause in v):
                    return False
                continue
            dv = doc.get(k)
            if isinstance(v, dict):
                if "$in" in v and dv not in v["$in"]:
                    return False
                if "$ne" in v and dv == v["$ne"]:
                    return False
                if "$exists" in v:
                    exists = k in doc
                    if bool(v["$exists"]) != exists:
                        return False
            elif dv != v:
                return False
        return True


class _FakeDB:
    def __init__(self):
        self._store: dict[str, list] = {}
        self._uniques = {
            C.COL_ORGANIZATIONS: [["domain"]],
            C.COL_MEMBERSHIPS: [["user_id", "org_id"]],
            C.COL_MODULE_ROLE_MAPPINGS: [["user_id", "org_id", "module"]],
            C.COL_PERMISSION_MATRIX: [["role_id", "module", "action"]],
            C.COL_RBAC_SESSIONS: [["session_id"]],
        }

    def __getitem__(self, name: str) -> _FakeCollection:
        return _FakeCollection(name, self._store, self._uniques.get(name))


class RbacBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.db = _FakeDB()
        self.get_db_patch = patch("app.rbac.org_service._db", return_value=self.db)
        self.get_db_patch2 = patch("app.rbac.roles._db", return_value=self.db)
        self.get_db_patch3 = patch("app.rbac.audit._db", return_value=self.db)
        self.get_db_patch4 = patch("app.database.get_db", return_value=self.db)
        self.get_db_patch5 = patch("app.rbac.role_cache._db", return_value=self.db)
        self.settings_patch = patch(
            "app.rbac.role_cache._settings",
            return_value=MagicMock(redis_url=None, rbac_role_cache_ttl_seconds=60),
        )
        for p in (
            self.get_db_patch,
            self.get_db_patch2,
            self.get_db_patch3,
            self.get_db_patch4,
            self.get_db_patch5,
            self.settings_patch,
        ):
            p.start()
        from app.rbac.role_cache import reset_role_cache_for_tests

        reset_role_cache_for_tests()

    def tearDown(self):
        for p in (
            self.get_db_patch,
            self.get_db_patch2,
            self.get_db_patch3,
            self.get_db_patch4,
            self.get_db_patch5,
            self.settings_patch,
        ):
            p.stop()

    def test_consumer_domain_blocklist(self):
        self.assertTrue(is_consumer_domain("gmail.com"))
        self.assertTrue(is_consumer_domain("outlook.com"))
        self.assertFalse(is_consumer_domain("acme.com"))

    def test_consumer_login_never_super_admin(self):
        result = resolve_org_on_login(
            user_id=str(ObjectId()),
            email="alice@gmail.com",
            display_name="Alice",
        )
        self.assertTrue(result.requires_org_review)
        self.assertIsNone(result.org_id)
        self.assertIsNone(result.role_id)
        requests = self.db[C.COL_ORG_CREATE_REQUESTS].find({})
        self.assertEqual(len(requests), 1)

    def test_first_corporate_user_becomes_super_admin(self):
        uid = str(ObjectId())
        result = resolve_org_on_login(
            user_id=uid,
            email="ceo@acme.com",
            display_name="CEO",
        )
        self.assertTrue(result.is_new_org)
        self.assertEqual(result.role_id, C.ROLE_SUPER_ADMIN)
        self.assertEqual(result.membership_status, C.MEMBERSHIP_ACTIVE)
        org = self.db[C.COL_ORGANIZATIONS].find_one({"domain": "acme.com"})
        self.assertIsNotNone(org)
        self.assertEqual(org["status"], C.ORG_STATUS_PROVISIONAL)

    def test_second_user_pending_not_second_super_admin(self):
        first = str(ObjectId())
        second = str(ObjectId())
        resolve_org_on_login(user_id=first, email="a@acme.com", display_name="A")
        result = resolve_org_on_login(
            user_id=second, email="b@acme.com", display_name="B"
        )
        self.assertFalse(result.is_new_org)
        self.assertEqual(result.membership_status, C.MEMBERSHIP_PENDING)
        self.assertIsNone(result.role_id)
        orgs = self.db[C.COL_ORGANIZATIONS].find({"domain": "acme.com"})
        self.assertEqual(len(orgs), 1)
        notices = self.db[C.COL_RBAC_NOTIFICATIONS].find({})
        self.assertGreaterEqual(len(notices), 1)

    def test_preassigned_role_activates_on_login(self):
        """Admin saves HR email into recruiter role before they sign in."""
        from app.rbac.org_service import ensure_hr_user_stub, upsert_membership
        from app.rbac.roles import set_module_role

        admin = str(ObjectId())
        boot = resolve_org_on_login(
            user_id=admin, email="ceo@acme.com", display_name="CEO"
        )
        org_id = boot.org_id
        # Simulate invite stub + matrix role
        with patch("app.rbac.org_service._db", return_value=self.db), patch(
            "app.database.get_db", return_value=self.db
        ):
            # ensure_hr_user_stub needs hr_users collection on fake db
            self.db._store.setdefault("hr_users", [])
            hr_id = ensure_hr_user_stub(
                email="hr@acme.com", org_id=org_id, name="HR Lead"
            )
            upsert_membership(
                user_id=hr_id,
                org_id=org_id,
                status=C.MEMBERSHIP_ACTIVE,
                email="hr@acme.com",
            )
            set_module_role(
                user_id=hr_id,
                org_id=org_id,
                module=C.MODULE_RECRUITMENT,
                role_id=C.ROLE_RECRUITER,
            )

        result = resolve_org_on_login(
            user_id=hr_id, email="hr@acme.com", display_name="HR Lead"
        )
        self.assertEqual(result.membership_status, C.MEMBERSHIP_ACTIVE)
        self.assertEqual(result.role_id, C.ROLE_RECRUITER)
        self.assertNotEqual(result.role_id, C.ROLE_SUPER_ADMIN)

    def test_race_loser_becomes_pending(self):
        """Simulate duplicate-key on org insert → join existing as pending."""
        winner = str(ObjectId())
        resolve_org_on_login(user_id=winner, email="a@race.com", display_name="A")
        # Pre-insert already done; second call finds existing org.
        loser = str(ObjectId())
        result = resolve_org_on_login(
            user_id=loser, email="b@race.com", display_name="B"
        )
        self.assertEqual(result.membership_status, C.MEMBERSHIP_PENDING)
        self.assertIsNone(result.role_id)


class RbacMatrixTests(unittest.TestCase):
    def setUp(self):
        self.db = _FakeDB()
        # Seed matrix rows into fake DB
        now = datetime.now(timezone.utc)
        for role_id, action, scope, setting_key, config_default, notes in _RECRUITMENT_ROWS:
            self.db[C.COL_PERMISSION_MATRIX].insert_one(
                {
                    "role_id": role_id,
                    "module": C.MODULE_RECRUITMENT,
                    "action": action,
                    "scope": scope,
                    "config_setting_key": setting_key,
                    "config_default": config_default,
                    "notes": notes,
                    "created_at": now,
                }
            )
        self.org_id = str(ObjectId())
        self.user_id = str(ObjectId())
        self.db[C.COL_ORGANIZATIONS].insert_one(
            {
                "_id": ObjectId(self.org_id),
                "domain": "matrix.test",
                "status": C.ORG_STATUS_PROVISIONAL,
                "settings": {},
            }
        )
        self.patches = [
            patch("app.rbac.org_service._db", return_value=self.db),
            patch("app.rbac.roles._db", return_value=self.db),
            patch("app.rbac.permissions.get_org", side_effect=lambda oid: self.db[C.COL_ORGANIZATIONS].find_one({"_id": ObjectId(oid)})),
            patch("app.rbac.role_cache._db", return_value=self.db),
            patch(
                "app.rbac.role_cache._settings",
                return_value=MagicMock(redis_url=None, rbac_role_cache_ttl_seconds=60),
            ),
        ]
        for p in self.patches:
            p.start()
        from app.rbac.role_cache import reset_role_cache_for_tests

        reset_role_cache_for_tests()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _map_role(self, role_id: str):
        self.db[C.COL_MODULE_ROLE_MAPPINGS].insert_one(
            {
                "user_id": self.user_id,
                "org_id": self.org_id,
                "module": C.MODULE_RECRUITMENT,
                "role_id": role_id,
            }
        )

    def test_super_admin_can_list_jobs_org_scope(self):
        self._map_role(C.ROLE_SUPER_ADMIN)
        d = can(
            user_id=self.user_id,
            org_id=self.org_id,
            module=C.MODULE_RECRUITMENT,
            action=C.ACTION_JOBS_LIST,
            membership_status=C.MEMBERSHIP_ACTIVE,
        )
        self.assertTrue(d.allowed)
        self.assertEqual(d.scope, C.SCOPE_ORG)

    def test_live_membership_overrides_stale_pending_session(self):
        """After admin assigns a role, can() uses DB not session pending flag."""
        self._map_role(C.ROLE_RECRUITER)
        self.db[C.COL_MEMBERSHIPS].insert_one(
            {
                "user_id": self.user_id,
                "org_id": self.org_id,
                "status": C.MEMBERSHIP_ACTIVE,
                "email": "r@matrix.test",
            }
        )
        d = can(
            user_id=self.user_id,
            org_id=self.org_id,
            module=C.MODULE_RECRUITMENT,
            action=C.ACTION_JOBS_CREATE,
            membership_status=C.MEMBERSHIP_PENDING,  # stale session
        )
        self.assertTrue(d.allowed)
        self.assertEqual(d.scope, C.SCOPE_OWN)
        self.assertEqual(d.role_id, C.ROLE_RECRUITER)

    def test_recruiter_cannot_delete_jobs(self):
        self._map_role(C.ROLE_RECRUITER)
        d = can(
            user_id=self.user_id,
            org_id=self.org_id,
            module=C.MODULE_RECRUITMENT,
            action=C.ACTION_JOBS_DELETE,
            membership_status=C.MEMBERSHIP_ACTIVE,
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.scope, C.SCOPE_NONE)

    def test_pending_member_denied_jobs_list(self):
        d = can(
            user_id=self.user_id,
            org_id=self.org_id,
            module=C.MODULE_RECRUITMENT,
            action=C.ACTION_JOBS_LIST,
            membership_status=C.MEMBERSHIP_PENDING,
        )
        self.assertFalse(d.allowed)


class RbacSessionTests(unittest.TestCase):
    def setUp(self):
        self.db = _FakeDB()
        self.patches = [
            patch("app.rbac.session_store._db", return_value=self.db),
            patch("app.rbac.audit._db", return_value=self.db),
            patch(
                "app.rbac.session_store._settings",
                return_value=MagicMock(
                    redis_url=None,
                    jwt_expire_minutes=60,
                    rbac_session_ttl_seconds=0,
                ),
            ),
        ]
        for p in self.patches:
            p.start()
        from app.rbac.session_store import reset_session_store_for_tests

        reset_session_store_for_tests()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        from app.rbac.session_store import reset_session_store_for_tests

        reset_session_store_for_tests()

    def test_create_get_delete_session(self):
        from app.rbac.session_store import get_session_store

        store = get_session_store()
        sid = store.create(
            user_id="u1",
            org_id="o1",
            membership_status=C.MEMBERSHIP_ACTIVE,
            email="a@acme.com",
        )
        self.assertTrue(sid)
        self.assertFalse("." in sid and sid.count(".") == 2)
        payload = store.get(sid)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["user_id"], "u1")
        self.assertEqual(payload["org_id"], "o1")
        self.assertTrue(store.delete(sid))
        self.assertIsNone(store.get(sid))

    def test_permission_refusal_writes_audit(self):
        from app.rbac.audit import audit_permission_refused

        audit_permission_refused(
            actor_id="u1",
            org_id="o1",
            module=C.MODULE_RECRUITMENT,
            permission_action=C.ACTION_JOBS_LIST,
            detail={"reason": "test"},
        )
        rows = self.db[C.COL_AUDIT_LOGS].find({"action": C.AUDIT_PERMISSION_REFUSED})
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
